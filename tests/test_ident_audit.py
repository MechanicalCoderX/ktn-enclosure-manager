"""A failed IDENT attempt must still be audited.

The audit log claims to record every write. It previously recorded only writes
that returned, so the cases most worth a trail of - the helper refusing, the
enclosure gone, a permission failure - were exactly the ones that left none.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from ktnmgr.enclosure.locate import LocateError
from ktnmgr.services.audit import AuditLog
from ktnmgr.services.ident import IdentManager


class ExplodingWriter:
    """Reads fine, refuses to write - like a helper that is down."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def read(self, enclosure_id: str, slot: int) -> bool:
        return False

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        raise self.error


class RefusingWriter:
    """Write 'succeeds' but the hardware does not honour it."""

    def read(self, enclosure_id: str, slot: int) -> bool:
        return False

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        return False


def make(tmp_path: Path, writer: object) -> tuple[IdentManager, AuditLog]:
    audit = AuditLog(tmp_path / "audit.log")
    manager = IdentManager(
        writer=writer,  # type: ignore[arg-type]
        audit=audit,
        state_path=tmp_path / "ident-state.json",
    )
    return manager, audit


@pytest.mark.asyncio
async def test_a_raising_write_is_audited(tmp_path: Path) -> None:
    manager, audit = make(tmp_path, ExplodingWriter(LocateError("helper is down")))

    with pytest.raises(LocateError):
        await manager.identify("0xabcd", 4, on=True, user="admin", duration_seconds=30)

    entries = audit.tail(10)
    assert len(entries) == 1, "a failed attempt left no audit trail"
    entry = entries[0]
    assert entry.operation == "IDENT_ON"
    assert entry.verification == "error"
    assert entry.user == "admin"
    assert entry.ses_slot == 4
    assert entry.bay == 5
    assert "helper is down" in (entry.detail or "")


@pytest.mark.asyncio
async def test_an_os_error_is_audited_too(tmp_path: Path) -> None:
    manager, audit = make(tmp_path, ExplodingWriter(OSError("permission denied")))

    with pytest.raises(OSError):
        await manager.identify("0xabcd", 0, on=False, user="admin")

    entries = audit.tail(10)
    assert len(entries) == 1
    assert entries[0].verification == "error"
    assert entries[0].operation == "IDENT_OFF"


@pytest.mark.asyncio
async def test_unhonoured_write_is_still_audited_as_failed(tmp_path: Path) -> None:
    manager, audit = make(tmp_path, RefusingWriter())

    with pytest.raises(LocateError):
        await manager.identify("0xabcd", 1, on=True, user="admin", duration_seconds=10)

    entries = audit.tail(10)
    assert len(entries) == 1
    assert entries[0].verification == "failed"


@pytest.mark.asyncio
async def test_no_record_is_kept_for_a_failed_request(tmp_path: Path) -> None:
    manager, _ = make(tmp_path, ExplodingWriter(LocateError("nope")))

    with pytest.raises(LocateError):
        await manager.identify("0xabcd", 2, on=True, user="admin", duration_seconds=30)

    assert manager.record_for("0xabcd", 2) is None


@pytest.mark.asyncio
async def test_ident_state_file_is_not_world_readable(tmp_path: Path) -> None:
    """It carries the username that raised each request."""

    class OkWriter:
        def read(self, enclosure_id: str, slot: int) -> bool:
            return False

        def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
            return on

    manager, _ = make(tmp_path, OkWriter())
    await manager.identify("0xabcd", 3, on=True, user="admin", duration_seconds=300)

    state = tmp_path / "ident-state.json"
    assert state.exists()
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
