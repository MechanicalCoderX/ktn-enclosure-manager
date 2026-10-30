"""The renewal watcher restarts on a real content change - and only then.

TrueNAS rewrites its certificate files in place every day whether or not
anything renewed, so the case that matters most is the same bytes with a new
mtime: that must never restart the app. The clock and the restart are fakes;
the files and the certificate parsing are real.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ktnmgr.services.audit import AuditLog
from ktnmgr.services.tls_watch import TlsWatcher, leaf_fingerprint
from tls_certs import make_expired_pair, make_pair


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).timestamp()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        cert, _ = make_pair(tmp_path / "gen", "current")
        self.source = tmp_path / "mounted" / "fullchain.pem"
        self.served = tmp_path / "run" / "cert.pem"
        self.source.parent.mkdir()
        self.served.parent.mkdir()
        self.source.write_bytes(cert.read_bytes())
        self.served.write_bytes(cert.read_bytes())
        self.restarts = 0
        self.clock = FakeClock()
        self.audit = AuditLog(tmp_path / "audit.log")
        self.watcher = TlsWatcher(
            self.source, self.served, interval=3600, settle=60,
            audit=self.audit, restart=self._restart, clock=self.clock,
        )

    def _restart(self) -> None:
        self.restarts += 1

    def rewrite_in_place(self, data: bytes) -> None:
        """What TrueNAS does: truncate and rewrite the same inode."""
        inode = self.source.stat().st_ino
        with self.source.open("r+b") as handle:
            handle.truncate(0)
            handle.write(data)
        assert self.source.stat().st_ino == inode

    def renewed_cert(self) -> bytes:
        cert, _ = make_pair(self.tmp_path / "gen", "renewed")
        return cert.read_bytes()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_identical_content_never_restarts(world: World) -> None:
    for _ in range(5):
        assert world.watcher.check() == 3600
        world.clock.advance(3600)
    assert world.restarts == 0
    assert world.watcher.reload_pending is False


def test_daily_same_bytes_rewrite_with_new_mtime_never_restarts(world: World) -> None:
    """TrueNAS's daily etc.generate ssl: new mtime, identical content."""
    same = world.source.read_bytes()
    for day in range(1, 4):
        world.rewrite_in_place(same)
        stamp = world.clock.now + day * 86400
        os.utime(world.source, (stamp, stamp))
        world.clock.advance(86400)
        world.watcher.check()
        world.clock.advance(60)
        world.watcher.check()
    assert world.restarts == 0
    # Control: the mtime really did move, so an mtime watcher would have fired.
    assert world.source.stat().st_mtime != world.served.stat().st_mtime


def test_changed_content_restarts_once_after_a_stable_settle(world: World) -> None:
    renewed = world.renewed_cert()
    world.rewrite_in_place(renewed)

    assert world.watcher.check() == 60          # change seen, settling
    assert world.watcher.reload_pending is True
    assert world.restarts == 0

    world.clock.advance(30)
    assert world.watcher.check() == pytest.approx(30)  # not yet
    assert world.restarts == 0

    world.clock.advance(30)
    world.watcher.check()
    assert world.restarts == 1

    for _ in range(3):  # the process is going down; never ask twice
        world.clock.advance(3600)
        world.watcher.check()
    assert world.restarts == 1

    entry = world.audit.tail(1)[0]
    assert entry.operation == "tls.reload"
    assert entry.detail is not None and leaf_fingerprint(renewed) in entry.detail
    assert "BEGIN" not in entry.detail


def test_a_change_during_settle_restarts_the_settle(world: World) -> None:
    world.rewrite_in_place(world.renewed_cert())
    world.watcher.check()
    world.clock.advance(50)
    world.rewrite_in_place(world.renewed_cert())  # changed again mid-settle
    world.watcher.check()
    world.clock.advance(50)                      # 100 s since first, 50 since second
    world.watcher.check()
    assert world.restarts == 0
    world.clock.advance(10)
    world.watcher.check()
    assert world.restarts == 1


@pytest.mark.parametrize(
    "content",
    [
        "truncated",
        b"this is not a certificate\n",
        b"",
    ],
)
def test_truncated_or_non_pem_content_never_restarts(world: World, content: object) -> None:
    if content == "truncated":
        renewed = world.renewed_cert()
        data = renewed[: len(renewed) // 2]
        # Keep the END marker out so it really is a partial write.
        assert b"-----END" not in data
    else:
        assert isinstance(content, bytes)
        data = content
    world.rewrite_in_place(data)
    for _ in range(5):
        world.watcher.check()
        world.clock.advance(120)
    assert world.restarts == 0
    assert world.watcher.reload_pending is False


def test_a_partial_chain_with_a_damaged_second_block_never_restarts(world: World) -> None:
    """A complete leaf followed by a half-written intermediate."""
    renewed = world.renewed_cert()
    data = renewed + renewed[: len(renewed) // 2]
    world.rewrite_in_place(data)
    for _ in range(3):
        world.watcher.check()
        world.clock.advance(120)
    assert world.restarts == 0
    # Control: the full two-block chain is accepted and does restart.
    world.rewrite_in_place(renewed + renewed)
    world.watcher.check()
    world.clock.advance(60)
    world.watcher.check()
    assert world.restarts == 1


def test_unreadable_source_disables_without_restarting(world: World) -> None:
    world.source.unlink()
    assert world.watcher.check() == 3600
    assert world.watcher.disabled_reason is not None
    assert "unreadable" in world.watcher.status()["watch_error"]
    world.clock.advance(3600)
    world.watcher.check()
    assert world.restarts == 0
    # Readable again (unchanged content): the watcher resumes, still no restart.
    world.source.write_bytes(world.served.read_bytes())
    world.watcher.check()
    assert world.watcher.disabled_reason is None
    assert world.restarts == 0


def test_days_left_and_status(world: World) -> None:
    world.watcher.refresh_expiry()
    status = world.watcher.status()
    assert status["enabled"] is True
    # make_pair issues 30-day certificates; floor() makes the first day 29.
    assert status["days_left"] in (29, 30)
    assert status["not_after"] is not None
    world.clock.advance(10 * 86400)
    assert world.watcher.days_left in (19, 20)


def test_days_left_is_negative_for_an_expired_served_cert(tmp_path: Path) -> None:
    cert, _ = make_expired_pair(tmp_path)
    watcher = TlsWatcher(cert, cert, interval=3600, settle=60, restart=lambda: None)
    watcher.refresh_expiry()
    assert watcher.days_left is not None and watcher.days_left < 0
    assert watcher.not_after == datetime(2020, 2, 1, tzinfo=UTC)


def test_the_watcher_never_opens_the_key(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the certificate path is ever read."""
    opened: list[str] = []
    real = Path.read_bytes
    world.rewrite_in_place(world.renewed_cert())

    def spy(self: Path) -> bytes:
        opened.append(str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    world.watcher.check()
    world.clock.advance(60)
    world.watcher.check()
    assert opened and set(opened) <= {str(world.source), str(world.served)}


class BrokenAudit:
    """An audit backend failing with something other than OSError.

    AuditLog.record swallows OSError itself, so this is hard to reach in
    practice - but if it ever happens, it must not cost the renewal.
    """

    def __init__(self) -> None:
        self.calls = 0

    def record(self, **_: object) -> None:
        self.calls += 1
        raise RuntimeError("audit backend broke")


def test_a_failing_audit_still_restarts_onto_the_renewal(world: World) -> None:
    """Review L1: the one-shot flag used to be set before the audit call, so
    an exception there skipped restart() and every later check returned early
    - the renewal was never installed for the rest of the process's life."""
    broken = BrokenAudit()
    world.watcher.audit = broken  # type: ignore[assignment]
    world.rewrite_in_place(world.renewed_cert())
    world.watcher.check()            # change seen: settle starts
    world.clock.advance(60)
    world.watcher.check()            # settled: audit raises, restart must still run
    assert broken.calls == 1
    assert world.restarts == 1
    # Still one-shot afterwards.
    for _ in range(3):
        world.clock.advance(3600)
        world.watcher.check()
    assert world.restarts == 1


# ------------------------------------- hardening pass 2026-10-30
# The hash and settle bookkeeping from every side: a source that goes back, goes
# bad or goes away mid-settle, an unreadable served copy, log de-duplication,
# and the loop around check().


def test_a_revert_during_settle_cancels_it_and_the_next_change_settles_afresh(
    world: World,
) -> None:
    """Renewed, then put back to the served bytes before the settle ended: no
    restart, and when the renewal reappears the clock starts again - the 30 s
    already spent on the first sighting must not count."""
    served_bytes = world.served.read_bytes()
    renewed = world.renewed_cert()
    world.rewrite_in_place(renewed)
    assert world.watcher.check() == 60
    assert world.watcher.reload_pending is True

    world.clock.advance(30)
    world.rewrite_in_place(served_bytes)
    assert world.watcher.check() == 3600
    assert world.watcher.reload_pending is False
    assert world.restarts == 0

    world.clock.advance(30)
    world.rewrite_in_place(renewed)
    assert world.watcher.check() == 60       # a fresh settle, not the 30 s left
    assert world.watcher.reload_pending is True

    world.clock.advance(30)
    world.watcher.check()
    assert world.restarts == 0
    world.clock.advance(30)
    world.watcher.check()
    assert world.restarts == 1


def test_unparseable_content_mid_settle_resets_the_settle(world: World) -> None:
    """A half-written file in the middle of a settle throws the first sighting
    away: the complete file that follows must itself be stable for a full settle."""
    renewed = world.renewed_cert()
    half = renewed[: len(renewed) // 2]
    assert b"-----END" not in half
    world.rewrite_in_place(renewed)
    world.watcher.check()
    world.clock.advance(50)
    world.rewrite_in_place(half)
    assert world.watcher.check() == 60       # the settle value, not the interval
    assert world.watcher.reload_pending is False

    world.clock.advance(10)
    world.rewrite_in_place(renewed)
    assert world.watcher.check() == 60       # fresh, though 60 s since the first
    assert world.restarts == 0
    world.clock.advance(59)
    world.watcher.check()
    assert world.restarts == 0
    world.clock.advance(1)
    world.watcher.check()
    assert world.restarts == 1


def test_an_unreadable_source_mid_settle_resets_the_settle(world: World) -> None:
    """The file vanishing during a settle also forgets the first sighting."""
    renewed = world.renewed_cert()
    world.rewrite_in_place(renewed)
    world.watcher.check()
    world.clock.advance(50)
    world.source.unlink()
    assert world.watcher.check() == 3600
    assert world.watcher.reload_pending is False

    world.source.write_bytes(renewed)        # the file is gone, so a plain write is fine
    assert world.watcher.check() == 60       # fresh settle
    assert world.watcher.reload_pending is True
    world.clock.advance(59)                  # 109 s since the FIRST sighting
    world.watcher.check()
    assert world.restarts == 0
    world.clock.advance(1)
    world.watcher.check()
    assert world.restarts == 1


def test_an_unreadable_served_copy_pauses_the_watcher_until_it_is_back(world: World) -> None:
    """Without the served copy there is nothing to compare with, so even a real
    renewal must not restart; when it returns the change settles normally."""
    served_bytes = world.served.read_bytes()
    world.served.unlink()
    world.rewrite_in_place(world.renewed_cert())

    assert world.watcher.check() == 3600
    reason = world.watcher.disabled_reason
    assert reason is not None and reason.startswith("served copy")
    assert "served copy" in world.watcher.status()["watch_error"]
    assert world.watcher.reload_pending is False
    for _ in range(3):
        world.clock.advance(3600)
        world.watcher.check()
    assert world.restarts == 0

    world.served.write_bytes(served_bytes)
    assert world.watcher.check() == 60
    assert world.watcher.disabled_reason is None
    assert world.watcher.reload_pending is True
    assert world.restarts == 0
    world.clock.advance(59)
    world.watcher.check()
    assert world.restarts == 0
    world.clock.advance(1)
    world.watcher.check()
    assert world.restarts == 1


def test_the_does_not_parse_warning_is_logged_once_per_distinct_content(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """A half-written file is rechecked every settle period; the log must not
    repeat itself for the same bytes, but a different bad file is news."""
    caplog.set_level(logging.WARNING, logger="ktnmgr.services.tls_watch")

    def parse_warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if "does not parse" in r.getMessage()]

    world.rewrite_in_place(b"bad content X\n")
    for _ in range(3):
        world.watcher.check()
        world.clock.advance(60)
    assert len(parse_warnings()) == 1

    world.rewrite_in_place(b"bad content Y\n")
    for _ in range(3):
        world.watcher.check()
        world.clock.advance(60)
    assert len(parse_warnings()) == 2


def test_the_paused_warning_is_logged_once_and_resumption_is_noted(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable source is rechecked every interval; warn once per reason,
    then say so when the watcher is back."""
    caplog.set_level(logging.INFO, logger="ktnmgr.services.tls_watch")

    def messages(level: int) -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno == level]

    world.source.unlink()
    for _ in range(3):
        world.watcher.check()
        world.clock.advance(3600)
    assert len([m for m in messages(logging.WARNING) if "paused" in m]) == 1
    assert not [m for m in messages(logging.INFO) if "resumed" in m]

    world.source.write_bytes(world.served.read_bytes())
    world.watcher.check()
    assert len([m for m in messages(logging.INFO) if "resumed" in m]) == 1
    assert len([m for m in messages(logging.WARNING) if "paused" in m]) == 1


def test_a_changed_chain_with_the_same_leaf_is_a_change_and_restarts(world: World) -> None:
    """Comparison is of the whole file's content, so an intermediate rotation is
    installed too - the entrypoint serves the full chain, not just the leaf."""
    served_bytes = world.served.read_bytes()
    extended = served_bytes + world.renewed_cert()
    assert leaf_fingerprint(extended) == leaf_fingerprint(served_bytes)
    world.rewrite_in_place(extended)
    assert world.watcher.check() == 60
    assert world.watcher.reload_pending is True
    world.clock.advance(59)
    world.watcher.check()
    assert world.restarts == 0
    world.clock.advance(1)
    world.watcher.check()
    assert world.restarts == 1


async def test_the_loop_survives_an_exception_from_check(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One surprise in check() must not end renewal watching for the process."""
    calls: list[int] = []

    def flaky_check() -> float:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 42

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(world.watcher, "check", flaky_check)
    monkeypatch.setattr("ktnmgr.services.tls_watch.asyncio.sleep", fake_sleep)
    caplog.set_level(logging.ERROR, logger="ktnmgr.services.tls_watch")
    with pytest.raises(asyncio.CancelledError):
        await world.watcher._loop()
    assert len(calls) == 2
    assert delays == [world.watcher.interval, 42]   # the failure waits a full interval
    assert world.watcher.interval == 3600
    assert "unexpected error" in caplog.text


async def test_start_is_idempotent_and_stop_cancels_the_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One task however often start() is called; stop() ends it and is safe to repeat."""
    monkeypatch.setattr(world.watcher, "check", lambda: 3600.0)
    world.watcher.start()
    task = world.watcher._task
    assert task is not None
    world.watcher.start()
    assert world.watcher._task is task
    await asyncio.sleep(0)                   # let the loop reach its sleep
    assert not task.done()

    await world.watcher.stop()
    assert task.cancelled()
    assert world.watcher._task is None
    await world.watcher.stop()               # a second stop is a no-op
    assert world.watcher._task is None


async def test_start_replaces_a_finished_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the loop task has ended, start() must make a new one rather than
    leaving the watcher silently dead."""
    monkeypatch.setattr(world.watcher, "check", lambda: 3600.0)
    world.watcher.start()
    first = world.watcher._task
    assert first is not None
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert first.done()
    world.watcher.start()
    second = world.watcher._task
    try:
        assert second is not None and second is not first
        assert not second.done()
    finally:
        await world.watcher.stop()
