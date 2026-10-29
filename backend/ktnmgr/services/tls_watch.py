"""Pick up a renewed TLS certificate by restarting the app.

uvicorn builds its TLS context once, at startup, from the copies the
entrypoint placed on the /run/ktn tmpfs. A renewed certificate therefore only
reaches clients after a restart, and only the root entrypoint can re-copy the
key (the web process cannot read the mounted original - that is the point).
So this watcher compares the mounted certificate with the served copy and,
once a change has settled, sends this process SIGTERM. uvicorn shuts down
gracefully, the container's restart policy (``unless-stopped`` on TrueNAS)
starts it again, and the entrypoint copies and checks the new pair.

Comparison is by content hash, never mtime. TrueNAS rewrites every
certificate file in place once a day whether or not anything was renewed
(its ``renew_certs`` periodic always ends in ``etc.generate ssl``), so an
mtime watcher would restart the app daily for nothing.

Two things it never does: read a private key (only certificates are hashed;
the mounted key is not even readable from this process), and restart on a
file it cannot read or parse. TrueNAS writes the certificate and then the
key without any atomicity, so a change must be stable across the settle
period and decode as a certificate before it counts; the entrypoint then
retries a still-mismatched pair before it gives up.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import signal
import ssl
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ktnmgr.services.audit import AuditLog

log = logging.getLogger(__name__)

#: Days before expiry at which the served certificate is logged as a warning.
EXPIRY_WARNING_DAYS = 14

_PEM_CERT = re.compile(
    rb"-----BEGIN CERTIFICATE-----\r?\n.+?\r?\n-----END CERTIFICATE-----", re.DOTALL
)


def default_restart() -> None:
    """Ask uvicorn (PID 1 in the container) for a graceful shutdown."""
    os.kill(os.getpid(), signal.SIGTERM)


def _parses_as_certificate(data: bytes) -> bool:
    """True when every PEM block decodes, so a half-written file never counts.

    load_verify_locations is the public parser: it rejects a truncated block,
    bad base64 and non-certificate content, and loads the whole chain.
    """
    if not _PEM_CERT.search(data):
        return False
    try:
        text = data.decode("ascii")
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=text)
    except (UnicodeDecodeError, ssl.SSLError, ValueError):
        return False
    return True


def leaf_fingerprint(data: bytes) -> str | None:
    """SHA-256 of the first certificate, formatted the way openssl prints it.

    So an audit entry can be compared directly with
    ``openssl x509 -noout -fingerprint -sha256``. Certificates are public;
    this is the only thing about them that is ever logged.
    """
    match = _PEM_CERT.search(data)
    if match is None:
        return None
    try:
        der = ssl.PEM_cert_to_DER_cert(match.group(0).decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _not_after(path: Path) -> datetime | None:
    """notAfter of the first certificate in ``path``, or None.

    ``_test_decode_cert`` is a private CPython helper, but it is the only
    stdlib route from a PEM file to its validity dates without a third-party
    library, so it is wrapped rather than relied on.
    """
    try:
        info = ssl._ssl._test_decode_cert(str(path))  # type: ignore[attr-defined]
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), UTC)
    except Exception:  # noqa: BLE001 - a missing date must never break serving
        return None


class TlsWatcher:
    """Restart the app when the mounted certificate's content really changes."""

    def __init__(
        self,
        source: Path,
        served: Path,
        interval: float,
        settle: float,
        *,
        audit: AuditLog | None = None,
        restart: Callable[[], None] = default_restart,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source = Path(source)
        self.served = Path(served)
        self.interval = interval
        self.settle = settle
        self.audit = audit
        self.restart = restart
        self.clock = clock

        self.not_after: datetime | None = None
        self.reload_pending = False
        self.disabled_reason: str | None = None

        self._pending: tuple[str, float] | None = None  # (source hash, first seen)
        self._unusable_hash: str | None = None
        self._restarted = False
        self._warned_days: int | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- state

    @property
    def days_left(self) -> int | None:
        if self.not_after is None:
            return None
        return int((self.not_after.timestamp() - self.clock()) // 86400)

    def status(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "enabled": True,
            "not_after": self.not_after.isoformat() if self.not_after else None,
            "days_left": self.days_left,
            "reload_pending": self.reload_pending,
            "watching": self.interval > 0,
        }
        if self.disabled_reason:
            body["watch_error"] = self.disabled_reason
        return body

    def refresh_expiry(self) -> None:
        """Re-read the served certificate's notAfter and warn as it nears."""
        self.not_after = _not_after(self.served)
        days = self.days_left
        if days is None or days > EXPIRY_WARNING_DAYS or days == self._warned_days:
            return
        self._warned_days = days
        if days < 0:
            log.warning(
                "the served TLS certificate EXPIRED on %s; browsers refuse it by name "
                "with no bypass while HSTS covers the host - renew it in TrueNAS, or "
                "reach the app by IP address over https and accept the warning",
                self.not_after.isoformat() if self.not_after else "?",
            )
        else:
            log.warning(
                "the served TLS certificate expires in %d day(s), on %s",
                days, self.not_after.isoformat() if self.not_after else "?",
            )

    # -------------------------------------------------------------- loop

    def _unreadable(self, what: str, path: Path, exc: OSError) -> float:
        reason = f"{what} {path} unreadable: {exc.strerror or type(exc).__name__}"
        if self.disabled_reason != reason:
            log.warning("TLS reload watcher paused: %s; a renewal will not be picked up", reason)
        self.disabled_reason = reason
        self._pending = None
        self.reload_pending = False
        return self.interval

    def check(self) -> float:
        """One comparison. Returns the seconds to wait before the next one."""
        self.refresh_expiry()
        try:
            source = self.source.read_bytes()
        except OSError as exc:
            return self._unreadable("certificate", self.source, exc)
        try:
            served = self.served.read_bytes()
        except OSError as exc:
            return self._unreadable("served copy", self.served, exc)
        if self.disabled_reason is not None:
            log.info("TLS reload watcher resumed: %s is readable again", self.source)
            self.disabled_reason = None

        source_hash = hashlib.sha256(source).hexdigest()
        if source_hash == hashlib.sha256(served).hexdigest():
            # The daily same-bytes rewrite lands here: nothing to do.
            self._pending = None
            self.reload_pending = False
            return self.interval

        if not _parses_as_certificate(source):
            # Mid-write, or something that is not a certificate. Never restart
            # onto it - the entrypoint would refuse it and the app would stop.
            if self._unusable_hash != source_hash:
                log.warning("%s changed but does not parse as a certificate; waiting", self.source)
                self._unusable_hash = source_hash
            self._pending = None
            self.reload_pending = False
            return self.settle

        now = self.clock()
        if self._pending is None or self._pending[0] != source_hash:
            self._pending = (source_hash, now)
            self.reload_pending = True
            log.info(
                "TLS certificate at %s changed; restarting in %ss if it stays unchanged",
                self.source, self.settle,
            )
            return self.settle
        remaining = self.settle - (now - self._pending[1])
        if remaining > 0:
            return remaining

        if self._restarted:
            return self.interval
        # The audit entry and the log line are best-effort: nothing in them
        # may stop the restart. The one-shot flag is set only right before
        # restart() for the same reason - set earlier, an exception in between
        # left it True with no restart, and every later check returned above,
        # so the renewal was never installed for the life of the process.
        try:
            old, new = leaf_fingerprint(served), leaf_fingerprint(source)
            if self.audit is not None:
                self.audit.record(
                    user="system:tls",
                    enclosure="-",
                    operation="tls.reload",
                    result="restart",
                    verification="n/a",
                    detail=f"served sha256 {old} -> renewed sha256 {new}",
                )
            log.warning(
                "renewed TLS certificate detected (sha256 %s); restarting so the "
                "entrypoint can install it - the container restart policy brings the "
                "app back", new,
            )
        except Exception:  # noqa: BLE001 - see above: never block the restart
            log.exception("TLS reload: audit/log failed; restarting anyway")
        self._restarted = True
        self.restart()
        return self.interval

    async def _loop(self) -> None:
        while True:
            try:
                delay = self.check()
            except Exception:  # noqa: BLE001 - the watcher must never die
                log.exception("unexpected error in the TLS reload watcher")
                delay = self.interval
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
