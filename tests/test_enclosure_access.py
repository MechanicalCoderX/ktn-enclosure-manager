"""Enclosure access serialisation.

The lock keeps an IDENT write and its settle read-back from being interleaved
with other enclosure traffic, so a reader cannot observe a half-applied locate
state. A regression here has no visible symptom, so these tests assert on
*exclusion itself* rather than on any output.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from ktnmgr.enclosure.access import default_lock_path, enclosure_access


def test_second_holder_waits_for_the_first(tmp_path: Path) -> None:
    """flock must actually exclude, not merely appear to."""
    lock = tmp_path / "enclosure.lock"
    order: list[str] = []
    first_inside = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with enclosure_access(lock):
            order.append("first-in")
            first_inside.set()
            release_first.wait(timeout=5)
            order.append("first-out")

    def hold_second() -> None:
        with enclosure_access(lock):
            order.append("second-in")

    t1 = threading.Thread(target=hold_first)
    t1.start()
    assert first_inside.wait(timeout=5)

    t2 = threading.Thread(target=hold_second)
    t2.start()
    # The second thread must still be blocked while the first holds the lock.
    time.sleep(0.2)
    assert order == ["first-in"], f"second holder was not excluded: {order}"

    release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["first-in", "first-out", "second-in"]


def test_nested_acquire_in_same_thread_does_not_deadlock(tmp_path: Path) -> None:
    """set_locate() takes the lock and then calls slot_dir(), which takes it
    again. flock is per-descriptor, so without re-entrancy that self-deadlocks."""
    lock = tmp_path / "enclosure.lock"
    with enclosure_access(lock) as outer:
        assert outer is True
        with enclosure_access(lock) as inner:
            assert inner is True
    # Still usable afterwards: the depth counter must have unwound.
    with enclosure_access(lock) as again:
        assert again is True


def test_unusable_lock_path_degrades_instead_of_raising(tmp_path: Path) -> None:
    """Availability beats silence: the lock only suppresses log noise, so a
    read-only or missing location must not take enclosure reporting down."""
    unusable = tmp_path / "no-such-dir" / "enclosure.lock"
    with enclosure_access(unusable) as held:
        assert held is False


def test_timeout_proceeds_unsynchronised(tmp_path: Path) -> None:
    lock = tmp_path / "enclosure.lock"
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with enclosure_access(lock):
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    assert holding.wait(timeout=5)
    try:
        started = time.monotonic()
        with enclosure_access(lock, timeout=0.2) as held:
            assert held is False
        assert time.monotonic() - started >= 0.2
    finally:
        release.set()
        t.join(timeout=5)


def _lock_is_held(path: Path) -> bool:
    """True if the lock file is currently flocked by someone.

    Uses a fresh descriptor, so it reports real kernel state rather than this
    module's re-entrancy bookkeeping.
    """
    import fcntl
    import os

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_ses_read_holds_the_lock_while_sg_ses_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ktnmgr.enclosure import ses as ses_module

    lock = tmp_path / "enclosure.lock"
    observed: list[bool] = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(*_args: object, **_kwargs: object) -> _Proc:
        observed.append(_lock_is_held(lock))
        return _Proc()

    monkeypatch.setattr(ses_module.subprocess, "run", fake_run)
    runner = ses_module.SesRunner(binary="/usr/bin/sg_ses", lock_path=lock)
    runner.read_page("/dev/sg16", "join")

    assert observed == [True], "sg_ses ran without holding the enclosure lock"
    assert not _lock_is_held(lock), "lock was not released after the read"


def test_slot_sweep_holds_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ktnmgr.enclosure import sysfs as sysfs_module

    lock = tmp_path / "enclosure.lock"
    observed: list[bool] = []
    real_read_text = sysfs_module._read_text

    def spy_read_text(path: Path) -> str | None:
        observed.append(_lock_is_held(lock))
        return real_read_text(path)

    monkeypatch.setattr(sysfs_module, "_read_text", spy_read_text)

    enclosure = tmp_path / "enc"
    slot = enclosure / "0"
    slot.mkdir(parents=True)
    (slot / "slot").write_text("0\n")
    (slot / "status").write_text("OK\n")

    backend = sysfs_module.SysfsEnclosureBackend(lock_path=lock)
    ref = type("Ref", (), {"sysfs_path": str(enclosure), "logical_id": "0xabc"})()
    backend.read_slots(ref)  # type: ignore[arg-type]

    assert observed, "no sysfs attribute was read"
    assert all(observed), "slot attributes were read without the enclosure lock"
    assert not _lock_is_held(lock), "lock was not released after the sweep"


def test_default_lock_path_follows_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KTN_ENCLOSURE_LOCK", raising=False)
    monkeypatch.setenv("KTN_DATA_DIR", "/somewhere")
    assert default_lock_path() == Path("/somewhere/enclosure.lock")


def test_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KTN_DATA_DIR", "/somewhere")
    monkeypatch.setenv("KTN_ENCLOSURE_LOCK", "/run/ktn/enclosure.lock")
    assert default_lock_path() == Path("/run/ktn/enclosure.lock")
