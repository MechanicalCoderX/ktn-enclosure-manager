"""TrueNAS API client: JSON-RPC over WebSocket, with an opt-in REST fallback.

Transport choice (spec §21, §48): TrueNAS 25.10 serves the current, supported
API as JSON-RPC 2.0 over a WebSocket at ``/api/current``. The legacy REST
``/api/v2.0`` surface is still present on 25.10.5 (it answers 401 rather than
404) but is deprecated and disappears in 26.04 - and it refuses role-scoped
keys outright (403 on every read that works over JSON-RPC). The fallback is
therefore opt-in via ``rest_fallback``: useful only to a deployment on a
full-access key, and harmful on the recommended least-privilege one, where it
turns a WebSocket blip into a false "rejected the API key" alarm.

The API key is held as a ``SecretStr`` and never logged, never echoed in an
error, and never sent to the browser (§21).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import time
from typing import Any

import httpx
import websockets
from pydantic import SecretStr

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0

#: How long to stay on the REST fallback after a WebSocket transport failure
#: before trying the preferred transport again.
WS_RETRY_COOLDOWN = 300.0


class TrueNASError(RuntimeError):
    """The TrueNAS API could not be reached or returned an error."""


class TrueNASClient:
    """Read-only client for the handful of methods this application needs."""

    #: Every method this client is capable of calling. The application never
    #: builds a method name from user input, and nothing here mutates state.
    ALLOWED_METHODS = frozenset(
        {
            "system.info",
            # Declared with authorization_required=False in the middleware, so
            # it works on a role-scoped key that system.info refuses - which
            # is exactly the recommended key. See SECURITY.md.
            "system.version",
            "disk.query",
            "disk.temperatures",
            "pool.query",
            # Present on 25.10.5. NOTE: there is no SMART attribute method -
            # `smart.test.results` was in this list but does not exist on the
            # appliance at all, so it could only ever have failed.
            "disk.temperature_alerts",
        }
    )

    def __init__(
        self,
        url: str,
        api_key: SecretStr | str,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        rest_fallback: bool = False,
    ) -> None:
        self.url = url.rstrip("/")
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self.verify_tls = verify_tls
        self.ca_bundle = ca_bundle
        self.timeout = timeout
        self.rest_fallback = rest_fallback
        self._socket: Any | None = None
        self._lock = asyncio.Lock()
        self._request_id = 0
        self._ws_retry_after = 0.0

    # ------------------------------------------------------------------ utils

    def __repr__(self) -> str:  # pragma: no cover - never leak the key
        return f"TrueNASClient(url={self.url!r}, verify_tls={self.verify_tls})"

    def _ssl_context(self) -> ssl.SSLContext | bool:
        if not self.url.startswith(("https://", "wss://")):
            return False
        if not self.verify_tls:
            # Explicit opt-out only; the default is verification on (§21).
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        if self.ca_bundle:
            return ssl.create_default_context(cafile=self.ca_bundle)
        return ssl.create_default_context()

    @property
    def _ws_url(self) -> str:
        base = self.url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/api/current"

    # -------------------------------------------------------------- transports

    async def _connect(self) -> Any:
        """Open a socket and authenticate it once."""
        ssl_context = self._ssl_context()
        connect_kwargs: dict[str, Any] = {"open_timeout": self.timeout}
        if ssl_context is not False:
            connect_kwargs["ssl"] = ssl_context

        socket = await websockets.connect(self._ws_url, **connect_kwargs)
        try:
            await self._ws_request(
                socket, self._next_id(), "auth.login_with_api_key",
                [self._api_key.get_secret_value()],
            )
        except BaseException:
            await self._discard_socket(socket)
            raise
        log.debug("TrueNAS WebSocket connected and authenticated")
        return socket

    async def _discard_socket(self, socket: Any | None = None) -> None:
        target = socket if socket is not None else self._socket
        if socket is None:
            self._socket = None
        if target is not None:
            with contextlib.suppress(Exception):
                await target.close()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def close(self) -> None:
        """Close the shared connection. Safe to call more than once."""
        async with self._lock:
            await self._discard_socket()

    async def _call_ws(self, method: str, params: list[Any]) -> Any:
        """Call over a REUSED, already-authenticated connection.

        Previously every call opened a fresh socket and ran
        auth.login_with_api_key, so a 20-second poll cycle produced three
        connections and three logins - roughly 13,000 authentications a day,
        each one an entry in the appliance's auth log.

        One request is in flight at a time (guarded by the lock) so replies
        cannot be mismatched across concurrent callers. A transport failure -
        including the idle timeout that will eventually close a pooled socket -
        drops the connection and retries once on a fresh one.
        """
        async with self._lock:
            for attempt in (1, 2):
                try:
                    if self._socket is None:
                        self._socket = await self._connect()
                    return await self._ws_request(
                        self._socket, self._next_id(), method, params
                    )
                except TrueNASError:
                    # Application-level refusal (bad key, unknown method).
                    # Retrying cannot help and would double the login attempts.
                    raise
                except Exception:
                    await self._discard_socket()
                    if attempt == 2:
                        raise
                    log.debug("TrueNAS WebSocket dropped; reconnecting once")
            raise TrueNASError("unreachable")  # pragma: no cover

    async def _ws_request(
        self, socket: Any, request_id: int, method: str, params: list[Any]
    ) -> Any:
        """Send one request and wait for the reply with that id.

        The whole exchange is time-boxed. ``recv()`` has no timeout of its own,
        so a connection that is open but never answers - a half-open socket
        after a network partition, or an appliance mid-restart - would block
        here forever. That call holds the client lock, so it would stall every
        TrueNAS poll for the life of the process and leave the UI showing
        indefinitely stale pool data with no error to explain it.
        """
        deadline = time.monotonic() + self.timeout
        await asyncio.wait_for(
            socket.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
            ),
            timeout=self.timeout,
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} did not answer within {self.timeout}s")
            raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            message = json.loads(raw)
            if message.get("id") != request_id:
                continue  # collected notification/event; not our reply
            if "error" in message:
                detail = message["error"]
                # Scrub: an auth failure echoes the method, never the key.
                raise TrueNASError(
                    f"{method} failed: {detail.get('message', 'unknown error')}"
                )
            result = message.get("result")
            if method == "auth.login_with_api_key" and result is not True:
                raise TrueNASError("TrueNAS rejected the API key")
            return result

    async def _call_rest(self, method: str, params: list[Any]) -> Any:
        """Fallback for the read methods that have a REST equivalent."""
        rest_paths = {
            "system.info": "/api/v2.0/system/info",
            "disk.query": "/api/v2.0/disk",
            "pool.query": "/api/v2.0/pool",
            "disk.temperatures": "/api/v2.0/disk/temperatures",
        }
        path = rest_paths.get(method)
        if path is None:
            raise TrueNASError(f"{method} has no REST fallback")

        verify: Any = self.ca_bundle if self.ca_bundle else self.verify_tls
        async with httpx.AsyncClient(verify=verify, timeout=self.timeout) as client:
            headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
            try:
                if method == "disk.temperatures":
                    response = await client.post(f"{self.url}{path}", headers=headers, json={})
                else:
                    response = await client.get(f"{self.url}{path}", headers=headers)
            except httpx.HTTPError as exc:
                # Containment, not decoration. httpx transport errors
                # (ConnectError, TimeoutException, ...) subclass httpx.HTTPError,
                # NOT OSError - and this is the one call site that runs outside
                # call()'s except clause. The poll handlers in services/state.py
                # catch (TrueNASError, OSError), so a raw httpx error would
                # escape to the poll loop's blanket handler and abandon the
                # whole tick, starving the unrelated SES/SMART polls at 1 Hz.
                # The exception's own message is safe to echo: it carries the
                # OS-level cause, never the Authorization header.
                raise TrueNASError(
                    f"{method}: REST request failed ({type(exc).__name__}: {exc})"
                ) from exc
            if response.status_code == 403:
                # Not a bad key. Role-scoped keys are authorised per method on
                # the JSON-RPC API only; the legacy REST surface refuses them
                # wholesale (measured on 25.10.5: the same key that reads
                # everything over /api/current gets 403 on every /api/v2.0
                # read). Saying "rejected the key" here sent operators off to
                # rotate a key that was fine.
                raise TrueNASError(
                    f"{method}: the legacy REST API refused this key (403). "
                    "Role-scoped keys only work over the JSON-RPC WebSocket; "
                    "this does not mean the key is bad."
                )
            if response.status_code == 401:
                raise TrueNASError("TrueNAS rejected the API key")
            if response.status_code >= 400:
                raise TrueNASError(f"{method} failed: HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                # A 200 with a non-JSON body (a proxy's HTML error page, a
                # captive portal) raises json.JSONDecodeError - a ValueError,
                # so it too slips past the (TrueNASError, OSError) handlers
                # in services/state.py. Same containment as above.
                raise TrueNASError(f"{method}: REST returned a non-JSON body") from exc

    # ------------------------------------------------------------------- calls

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Call an allow-listed read-only method, preferring the WebSocket API."""
        if method not in self.ALLOWED_METHODS:
            raise TrueNASError(f"method {method!r} is not allow-listed")
        params = params or []

        if time.monotonic() >= self._ws_retry_after:
            try:
                return await self._call_ws(method, params)
            except TrueNASError:
                raise
            except Exception as exc:  # transport-level failure
                if not self.rest_fallback:
                    # Off by default, for two reasons that were both measured.
                    # A role-scoped key gets 403 on every legacy REST read
                    # while working fine over JSON-RPC, so on the recommended
                    # key the fallback cannot succeed - it can only convert a
                    # transient WebSocket blip into a false "rejected the API
                    # key" alarm. And /api/v2.0 is removed in TrueNAS 26.04,
                    # at which point this path stops existing anyway.
                    raise TrueNASError(
                        f"{method}: the JSON-RPC WebSocket is unreachable "
                        f"({type(exc).__name__}). The legacy REST fallback is "
                        "disabled (KTN_TRUENAS_REST_FALLBACK); it only helps "
                        "deployments using a full-access key."
                    ) from exc
                # Time-boxed rather than latched forever: a WebSocket outage
                # used to disable the preferred transport for the lifetime of
                # the process, so a brief blip meant permanent REST.
                self._ws_retry_after = time.monotonic() + WS_RETRY_COOLDOWN
                log.warning(
                    "JSON-RPC WebSocket unavailable (%s); using REST v2.0 for %.0fs",
                    type(exc).__name__, WS_RETRY_COOLDOWN,
                )

        return await self._call_rest(method, params)

    async def system_info(self) -> dict[str, Any]:
        info = await self.call("system.info")
        return info if isinstance(info, dict) else {}

    async def version(self) -> str:
        """The appliance's version string, on any key.

        ``system.info`` accepts only READONLY_ADMIN/SHARING_ADMIN, so on the
        recommended least-privilege key it is denied - and holding a broader
        key just to read a version string is the wrong trade. This method has
        no authorization requirement in the middleware, so it works everywhere.
        """
        result = await self.call("system.version")
        return result if isinstance(result, str) else ""

    async def disks(self) -> list[dict[str, Any]]:
        """Disk inventory.

        Note: ``disk.query`` returns ``pool: None`` even for pooled disks on
        25.10.5, so pool membership is derived from ``pool.query`` topology
        instead - see ``ktnmgr.truenas.correlate``.
        """
        result = await self.call("disk.query")
        return result if isinstance(result, list) else []

    async def pools(self) -> list[dict[str, Any]]:
        result = await self.call("pool.query")
        return result if isinstance(result, list) else []

    async def temperature_alerts(self, names: list[str]) -> list[dict[str, Any]]:
        """Disks TrueNAS has raised a temperature alert for.

        ``names`` is required by the appliance - calling this with no argument
        returns ``[EINVAL] names: Field required``, so the previous no-argument
        wrapper could never have succeeded. It went unnoticed because nothing
        called it.

        Each entry is an ``alert.list`` record of class ``DiskTemperatureTooHot``
        whose ``args.device`` is ``/dev/<name>``.

        This is the only disk-health signal the 25.10 API exposes beyond raw
        temperature; there is no endpoint for SMART overall status or power-on
        hours. See SmartInfo in models.py.
        """
        if not names:
            return []
        try:
            result = await self.call("disk.temperature_alerts", [list(names)])
        except TrueNASError as exc:
            # No REST equivalent exists, so this is expected while the client
            # is in its WebSocket cooldown. Temperature alerting is
            # supplementary - losing it must not take the SMART poll down.
            log.debug("temperature alerts unavailable: %s", exc)
            return []
        return result if isinstance(result, list) else []

    async def temperatures(self) -> dict[str, float | None]:
        result = await self.call("disk.temperatures")
        if not isinstance(result, dict):
            return {}
        return {str(k): (float(v) if isinstance(v, (int, float)) else None)
                for k, v in result.items()}
