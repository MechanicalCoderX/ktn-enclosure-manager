"""Append-only audit log for every write action (spec §34).

Records IDENT on/off, including automatic clearing performed by the timer
engine with no user present. Never records secrets.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from ktnmgr.models import AuditEntry

log = logging.getLogger(__name__)

#: Field names that must never appear in an audit record, whatever a caller
#: passes as `detail`.
_FORBIDDEN = ("api_key", "apikey", "password", "passwd", "secret", "token", "session")


class AuditLog:
    """JSON-lines audit log. Appends are serialised across threads."""

    def __init__(self, path: Path, max_tail: int = 500) -> None:
        self.path = Path(path)
        self.max_tail = max_tail
        self._lock = threading.Lock()

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
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:  # never let auditing failure break the operation
                log.error("could not append to audit log %s: %s", self.path, exc)
        log.info(
            "audit user=%s enclosure=%s bay=%s op=%s result=%s verification=%s",
            user, enclosure, bay, operation, result, verification,
        )
        return entry

    def tail(self, limit: int = 100) -> list[AuditEntry]:
        limit = max(1, min(limit, self.max_tail))
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[AuditEntry] = []
        for line in lines[-limit:]:
            try:
                entries.append(AuditEntry.model_validate_json(line))
            except ValueError:
                continue
        entries.reverse()
        return entries
