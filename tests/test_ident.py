"""IDENT timer engine and startup reconciliation tests (spec §26, §27, §37)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ktnmgr.enclosure.locate import LocateError
from ktnmgr.services.audit import AuditLog
from ktnmgr.services.ident import (
    ORIGIN_APP,
    ORIGIN_EXTERNAL,
    IdentManager,
    IdentRecord,
    IdentState,
)

LOGICAL_ID = "0x50060480aabbcc00"


class FakeWriter:
    """In-memory locate writer. `honour=False` simulates hardware that accepts
    the write but does not change state, which must fail verification."""

    def __init__(self, honour: bool = True) -> None:
        self.state: dict[tuple[str, int], bool] = {}
        self.honour = honour
        self.writes: list[tuple[str, int, bool]] = []

    def read(self, enclosure_id: str, slot: int) -> bool:
        return self.state.get((enclosure_id, slot), False)

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        self.writes.append((enclosure_id, slot, on))
        if self.honour:
            self.state[(enclosure_id, slot)] = on
        return self.state.get((enclosure_id, slot), False)


@pytest.fixture
def manager(tmp_path: Path) -> IdentManager:
    return IdentManager(
        writer=FakeWriter(),
        audit=AuditLog(tmp_path / "audit.log"),
        state_path=tmp_path / "ident.json",
        tick_seconds=0.05,
    )


async def test_identify_on_and_off(manager: IdentManager) -> None:
    record = await manager.identify(LOGICAL_ID, 0, on=True, user="admin", duration_seconds=None)
    assert record is not None
    assert record.origin == ORIGIN_APP
    assert record.expires_at is None
    assert manager.writer.read(LOGICAL_ID, 0) is True

    assert await manager.identify(LOGICAL_ID, 0, on=False, user="admin") is None
    assert manager.writer.read(LOGICAL_ID, 0) is False


async def test_rejects_undeclared_duration(manager: IdentManager) -> None:
    with pytest.raises(LocateError):
        await manager.identify(LOGICAL_ID, 0, on=True, user="admin", duration_seconds=17)


@pytest.mark.parametrize("duration", [10, 30, 60, 300, None])
async def test_offered_durations_accepted(manager: IdentManager, duration: int | None) -> None:
    record = await manager.identify(
        LOGICAL_ID, 1, on=True, user="admin", duration_seconds=duration
    )
    assert record is not None
    assert (record.expires_at is None) == (duration is None)


async def test_verification_failure_raises_and_is_audited(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.log")
    manager = IdentManager(FakeWriter(honour=False), audit, tmp_path / "ident.json")
    with pytest.raises(LocateError, match="verification failed"):
        await manager.identify(LOGICAL_ID, 0, on=True, user="admin")
    entries = audit.tail()
    assert entries[0].verification == "failed"
    assert entries[0].operation == "IDENT_ON"


async def test_timer_clears_automatically(tmp_path: Path) -> None:
    """§26: the clear must happen server-side with no client involvement."""
    writer = FakeWriter()
    manager = IdentManager(writer, AuditLog(tmp_path / "a.log"), tmp_path / "i.json",
                           tick_seconds=0.05)
    manager.start()
    try:
        record = await manager.identify(LOGICAL_ID, 3, on=True, user="admin",
                                        duration_seconds=10)
        assert record is not None
        # Fast-forward rather than sleeping for the real duration.
        manager._records[(LOGICAL_ID, 3)].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not writer.read(LOGICAL_ID, 3):
                break
        assert writer.read(LOGICAL_ID, 3) is False
        assert manager.record_for(LOGICAL_ID, 3) is None
    finally:
        await manager.stop()


class FailingWriter(FakeWriter):
    """FakeWriter whose writes raise `error` for the first `failures` calls
    (a transient outage such as the helper restarting), or forever when
    `failures` is None (enclosure permanently gone)."""

    def __init__(self, error: Exception, failures: int | None = None) -> None:
        super().__init__()
        self.error = error
        self.failures = failures
        self.failed = 0

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        if self.failures is None or self.failed < self.failures:
            self.failed += 1
            raise self.error
        return super().write(enclosure_id, slot, on)


def _expired_record(ses_slot: int) -> IdentRecord:
    return IdentRecord(
        enclosure_id=LOGICAL_ID, ses_slot=ses_slot, created_by="admin",
        created_at=datetime.now(UTC) - timedelta(seconds=20),
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
    )


async def test_expired_clear_retries_transient_failure(tmp_path: Path) -> None:
    """A helper restart coinciding with expiry must not orphan the LED: the
    failed auto-clear is retried, not dropped on the first LocateError."""
    audit = AuditLog(tmp_path / "a.log")
    writer = FailingWriter(LocateError("helper socket unreachable"), failures=2)
    writer.state[(LOGICAL_ID, 3)] = True
    manager = IdentManager(writer, audit, tmp_path / "i.json",
                           tick_seconds=0.02, clear_backoff_seconds=0.01)
    manager._records[(LOGICAL_ID, 3)] = _expired_record(3)
    manager.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if manager.record_for(LOGICAL_ID, 3) is None:
                break
    finally:
        await manager.stop()
    assert writer.failed == 2, "both transient failures should have been consumed"
    assert writer.read(LOGICAL_ID, 3) is False, "third attempt should have cleared the LED"
    assert manager.record_for(LOGICAL_ID, 3) is None
    cleared = [e for e in audit.tail()
               if e.user == "system:timer" and e.operation == "IDENT_OFF"
               and e.verification == "success"]
    assert len(cleared) == 1, "the successful auto-clear must be audited exactly once"


async def test_expired_clear_gives_up_after_bounded_attempts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A permanently failing clear (here a detached enclosure, which surfaces
    as LookupError rather than LocateError) must stop after the bounded
    attempts and drop the record with an operator-visible orphaned-LED
    warning - not retry every tick forever, and not drop silently."""
    from ktnmgr.enclosure.sysfs import EnclosureNotFoundError

    writer = FailingWriter(EnclosureNotFoundError("enclosure detached"))
    writer.state[(LOGICAL_ID, 4)] = True
    manager = IdentManager(writer, AuditLog(tmp_path / "a.log"), tmp_path / "i.json",
                           tick_seconds=0.02, clear_max_attempts=3,
                           clear_backoff_seconds=0.01)
    manager._records[(LOGICAL_ID, 4)] = _expired_record(4)
    with caplog.at_level("WARNING", logger="ktnmgr.services.ident"):
        manager.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.02)
                if manager.record_for(LOGICAL_ID, 4) is None:
                    break
        finally:
            await manager.stop()
    assert writer.failed == 3, "exactly clear_max_attempts write attempts"
    assert manager.record_for(LOGICAL_ID, 4) is None, "record dropped after giving up"
    give_up = [r for r in caplog.records
               if r.levelname == "ERROR" and "LED may still be lit" in r.getMessage()]
    assert give_up, "giving up must be loud - the LED is likely still lit"


async def test_timer_clear_is_audited_as_system(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.log")
    manager = IdentManager(FakeWriter(), audit, tmp_path / "i.json", tick_seconds=0.05)
    manager.start()
    try:
        await manager.identify(LOGICAL_ID, 4, on=True, user="admin", duration_seconds=10)
        manager._records[(LOGICAL_ID, 4)].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        for _ in range(40):
            await asyncio.sleep(0.05)
            if manager.record_for(LOGICAL_ID, 4) is None:
                break
    finally:
        await manager.stop()
    assert any(e.user == "system:timer" and e.operation == "IDENT_OFF" for e in audit.tail())


# ------------------------------------------------------------- reconciliation


async def test_reconcile_leaves_external_ident_alone(manager: IdentManager) -> None:
    """§27: an LED lit by someone else must NOT be cleared at startup."""
    manager.writer.state[(LOGICAL_ID, 5)] = True
    await manager.reconcile({(LOGICAL_ID, 5): True})
    assert manager.writer.read(LOGICAL_ID, 5) is True
    assert manager.writer.writes == []
    origin, _ = manager.describe(LOGICAL_ID, 5, locate_on=True)
    assert origin == ORIGIN_EXTERNAL


async def test_reconcile_clears_only_our_expired_request(tmp_path: Path) -> None:
    writer = FakeWriter()
    writer.state[(LOGICAL_ID, 6)] = True   # ours, expired
    writer.state[(LOGICAL_ID, 7)] = True   # someone else's
    state = IdentState(records=[
        IdentRecord(
            enclosure_id=LOGICAL_ID, ses_slot=6, created_by="admin",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
            expires_at=datetime.now(UTC) - timedelta(minutes=9),
        )
    ])
    path = tmp_path / "ident.json"
    path.write_text(state.model_dump_json())

    manager = IdentManager(writer, AuditLog(tmp_path / "a.log"), path)
    await manager.reconcile({(LOGICAL_ID, 6): True, (LOGICAL_ID, 7): True})

    assert writer.read(LOGICAL_ID, 6) is False, "our expired request should be cleared"
    assert writer.read(LOGICAL_ID, 7) is True, "external request must be untouched"


async def test_reconcile_keeps_unexpired_request_across_restart(tmp_path: Path) -> None:
    """§37: a container restart during a timed IDENT must not drop the timer."""
    writer = FakeWriter()
    writer.state[(LOGICAL_ID, 8)] = True
    path = tmp_path / "ident.json"
    path.write_text(IdentState(records=[
        IdentRecord(
            enclosure_id=LOGICAL_ID, ses_slot=8, created_by="admin",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=4),
        )
    ]).model_dump_json())

    manager = IdentManager(writer, AuditLog(tmp_path / "a.log"), path)
    await manager.reconcile({(LOGICAL_ID, 8): True})

    assert writer.read(LOGICAL_ID, 8) is True
    record = manager.record_for(LOGICAL_ID, 8)
    assert record is not None and record.origin == ORIGIN_APP


async def test_reconcile_drops_record_for_externally_cleared_led(tmp_path: Path) -> None:
    path = tmp_path / "ident.json"
    path.write_text(IdentState(records=[
        IdentRecord(enclosure_id=LOGICAL_ID, ses_slot=9, created_by="admin",
                    created_at=datetime.now(UTC))
    ]).model_dump_json())
    manager = IdentManager(FakeWriter(), AuditLog(tmp_path / "a.log"), path)
    await manager.reconcile({(LOGICAL_ID, 9): False})
    assert manager.record_for(LOGICAL_ID, 9) is None


async def test_state_survives_manager_restart(tmp_path: Path) -> None:
    writer = FakeWriter()
    path = tmp_path / "ident.json"
    first = IdentManager(writer, AuditLog(tmp_path / "a.log"), path)
    await first.identify(LOGICAL_ID, 2, on=True, user="admin", duration_seconds=300)

    second = IdentManager(writer, AuditLog(tmp_path / "a.log"), path)
    await second.reconcile({(LOGICAL_ID, 2): True})
    record = second.record_for(LOGICAL_ID, 2)
    assert record is not None
    assert record.created_by == "admin"


def test_describe_reports_nothing_when_dark(manager: IdentManager) -> None:
    assert manager.describe(LOGICAL_ID, 0, locate_on=False) == (None, None)


def test_audit_log_rotates_instead_of_growing_forever(tmp_path: Path) -> None:
    """A homelab app dataset is small; an append-only log must not fill it."""
    audit = AuditLog(tmp_path / "audit.log", max_bytes=2048)
    for _ in range(200):
        audit.record(user="u", enclosure="0x1", operation="IDENT_ON", verification="success")
    assert (tmp_path / "audit.log").stat().st_size < 2048 * 3
    assert (tmp_path / "audit.log.1").exists(), "previous generation should be kept"


def test_rate_limiter_prunes_stale_addresses() -> None:
    """Otherwise the dict grows one entry per source address, forever."""
    from ktnmgr.services.auth import RateLimiter

    limiter = RateLimiter(limit=5, window_seconds=0)
    for i in range(500):
        limiter.check(f"10.0.0.{i}")
    assert len(limiter._hits) < 50, "stale addresses should have been pruned"
