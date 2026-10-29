"""The renewal watcher restarts on a real content change - and only then.

TrueNAS rewrites its certificate files in place every day whether or not
anything renewed, so the case that matters most is the same bytes with a new
mtime: that must never restart the app. The clock and the restart are fakes;
the files and the certificate parsing are real.
"""

from __future__ import annotations

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
