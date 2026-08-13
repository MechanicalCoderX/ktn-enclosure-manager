"""TrueNAS API client: JSON-RPC over WebSocket, with REST v2.0 as fallback.

Transport choice (spec §21, §48): TrueNAS 25.10 serves the current, supported
API as JSON-RPC 2.0 over a WebSocket at ``/api/current``. The legacy REST
``/api/v2.0`` surface is still present on 25.10.5 (it answers 401 rather than
404) but is deprecated, so it is used only as a fallback when the WebSocket is
unavailable. Both were confirmed present on the target before this was written.

The API key is held as a ``SecretStr`` and never logged, never echoed in an
error, and never sent to the browser (§21).
"""

from __future__ import annotations

import json
import logging
import ssl
from typing import Any

import httpx
import websockets
from pydantic import SecretStr

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0


class TrueNASError(RuntimeError):
    """The TrueNAS API could not be reached or returned an error."""


class TrueNASClient:
    """Read-only client for the handful of methods this application needs."""

    #: Every method this client is capable of calling. The application never
    #: builds a method name from user input, and nothing here mutates state.
    ALLOWED_METHODS = frozenset(
        {
            "system.info",
            "disk.query",
            "disk.temperatures",
            "pool.query",
            "smart.test.results",
        }
    )

    def __init__(
        self,
        url: str,
        api_key: SecretStr | str,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self.verify_tls = verify_tls
        self.ca_bundle = ca_bundle
        self.timeout = timeout
        self._ws_failed = False

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

    async def _call_ws(self, method: str, params: list[Any]) -> Any:
        ssl_context = self._ssl_context()
        connect_kwargs: dict[str, Any] = {"open_timeout": self.timeout}
        if ssl_context is not False:
            connect_kwargs["ssl"] = ssl_context

        async with websockets.connect(self._ws_url, **connect_kwargs) as socket:
            await self._ws_request(socket, 1, "auth.login_with_api_key", [
                self._api_key.get_secret_value()
            ])
            return await self._ws_request(socket, 2, method, params)

    async def _ws_request(
        self, socket: Any, request_id: int, method: str, params: list[Any]
    ) -> Any:
        await socket.send(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        )
        while True:
            raw = await socket.recv()
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
            if method == "disk.temperatures":
                response = await client.post(f"{self.url}{path}", headers=headers, json={})
            else:
                response = await client.get(f"{self.url}{path}", headers=headers)
            if response.status_code == 401:
                raise TrueNASError("TrueNAS rejected the API key")
            if response.status_code >= 400:
                raise TrueNASError(f"{method} failed: HTTP {response.status_code}")
            return response.json()

    # ------------------------------------------------------------------- calls

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Call an allow-listed read-only method, preferring the WebSocket API."""
        if method not in self.ALLOWED_METHODS:
            raise TrueNASError(f"method {method!r} is not allow-listed")
        params = params or []

        if not self._ws_failed:
            try:
                return await self._call_ws(method, params)
            except TrueNASError:
                raise
            except Exception as exc:  # transport-level: fall back to REST
                log.warning(
                    "JSON-RPC WebSocket unavailable (%s); falling back to REST v2.0",
                    type(exc).__name__,
                )
                self._ws_failed = True

        return await self._call_rest(method, params)

    async def system_info(self) -> dict[str, Any]:
        info = await self.call("system.info")
        return info if isinstance(info, dict) else {}

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

    async def temperatures(self) -> dict[str, float | None]:
        result = await self.call("disk.temperatures")
        if not isinstance(result, dict):
            return {}
        return {str(k): (float(v) if isinstance(v, (int, float)) else None)
                for k, v in result.items()}
