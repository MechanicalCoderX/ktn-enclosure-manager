"""Append-only audit log for every write action (spec §34).

Records IDENT on/off, including automatic clearing performed by the timer
engine with no user present. Never records secrets.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from ktnmgr.models import AuditEntry

log = logging.getLogger(__name__)

#: Tail-read window sizing. An audit entry is a few hundred bytes; these only
#: affect how many reads it takes to find the requested number of lines.
_TAIL_CHUNK_BYTES = 64 * 1024
_ASSUMED_ENTRY_BYTES = 512

#: Field names that must never appear in an audit record, whatever a caller
#: passes as `detail`.
_FORBIDDEN = ("api_key", "apikey", "password", "passwd", "secret", "token", "session")


class AuditLog:
    """JSON-lines audit log. Appends are serialised across threads."""

    def __init__(
        self, path: Path, max_tail: int = 500, max_bytes: int = 5 * 1024 * 1024
    ) -> None:
        self.path = Path(path)
        self.max_tail = max_tail
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def _rotate_if_needed(self) -> None:
        """Keep one previous generation, so the log cannot grow without bound.

        An append-only file on a small app dataset will otherwise grow forever.
        Rotation is best-effort: failing to rotate must never prevent an
        operation from being recorded.
        """
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except OSError as exc:
            log.warning("could not rotate audit log: %s", exc)

    def _scrub(self, detail: str | None) -> str | None:
        if not detail:
            return detail
        lowered = detail.lower()
        if any(token in lowered for token in _FORBIDDEN):
            return "[redacted: detail mentioned a credential field]"
        return detail[:500]

    def record(
        self,
        *,
        user: str,
        enclosure: str,
        operation: str,
        bay: int | None = None,
        ses_slot: int | None = None,
        serial: str | None = None,
        previous: str | None = None,
        result: str | None = None,
        verification: str = "unknown",
        detail: str | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            timestamp=datetime.now(UTC),
            user=user,
            enclosure=enclosure,
            bay=bay,
            ses_slot=ses_slot,
            serial=serial,
            operation=operation,
            previous=previous,
            result=result,
            verification=verification,
            detail=self._scrub(detail),
        )
        line = entry.model_dump_json()
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                # 0600 at creation: entries carry usernames and drive serials,
                # and the mode only applies when this call creates the file.
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:  # never let auditing failure break the operation
                log.error("could not append to audit log %s: %s", self.path, exc)
        log.info(
            "audit user=%s enclosure=%s bay=%s op=%s result=%s verification=%s",
            user, enclosure, bay, operation, result, verification,
        )
        return entry

    def tail(self, limit: int = 100) -> list[AuditEntry]:
        """Most recent entries, newest first.

        Reads from the end of the file rather than loading all of it. The log
        rotates at 5 MB, so the old ``read_text().splitlines()`` pulled up to
        5 MB into memory and built a list of every line on every request, only
        to discard all but the last hundred.
        """
        limit = max(1, min(limit, self.max_tail))
        try:
            with self.path.open("rb") as handle:
                chunk = self._read_tail_bytes(handle, limit)
        except OSError:
            return []

        lines = chunk.decode("utf-8", errors="replace").splitlines()
        entries: list[AuditEntry] = []
        for line in lines[-limit:]:
            try:
                entries.append(AuditEntry.model_validate_json(line))
            except ValueError:
                # A truncated first line is expected when the window lands
                # mid-record, and a torn write should not break the view.
                continue
        entries.reverse()
        return entries

    @staticmethod
    def _read_tail_bytes(handle: BinaryIO, limit: int) -> bytes:
        """Read back from EOF until `limit` complete lines are in hand.

        Doubles the window rather than guessing an entry size, so an unusually
        long record cannot make this silently return too few entries.
        """
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        window = min(size, max(_TAIL_CHUNK_BYTES, limit * _ASSUMED_ENTRY_BYTES))

        while True:
            handle.seek(size - window)
            chunk = handle.read(window)
            # `> limit` because the first line is probably a partial record.
            if window >= size or chunk.count(b"\n") > limit:
                return chunk
            window = min(size, window * 2)
