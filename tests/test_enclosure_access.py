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


def test_lock_is_openable_by_a_second_uid(tmp_path: Path) -> None:
    """The mode must let *both* processes in.

    The helper runs as root and the web process as uid 1000, either may create
    the file first, and the container drops CAP_DAC_OVERRIDE - so root is
    subject to the mode exactly like uid 1000. A 0600 lock locks one of them
    out, which is not hypothetical: tightening it to 0600 made the helper fail
    with EACCES and silently disabled serialisation.
    """
    import stat

    lock = tmp_path / "enclosure.lock"
    with enclosure_access(lock) as held:
        assert held is True

    mode = stat.S_IMODE(lock.stat().st_mode)
    assert mode & stat.S_IRUSR and mode & stat.S_IWUSR
    assert mode & stat.S_IROTH and mode & stat.S_IWOTH, (
        f"mode {mode:o} would lock out whichever process did not create it"
    )


# --------------------------------------------------- a substituted lock path
#
# The root helper opens this path on every poll, and its directory (/run/ktn)
# is writable by the web process's gid with no sticky bit. Whatever a
# compromised web process leaves at the path must therefore be refused, and
# refused before root touches it: no chmod of another file, no file created
# somewhere else, no open that blocks. Refusal takes the same degraded path
# as any other unusable lock (yield False, proceed unsynchronised), so no
# caller has a new failure to handle.


def _mode(path: Path) -> int:
    """Permission bits of the entry itself, never of what a link points at."""
    import os
    import stat

    return stat.S_IMODE(os.lstat(path).st_mode)


def test_symlink_at_lock_path_is_refused_and_target_mode_unchanged(tmp_path: Path) -> None:
    """The reviewed primitive: root followed the link and fchmod'ed the target
    to 0666. Root owns the target, so the chmod needs no capability at all."""
    victim = tmp_path / "victim"
    victim.write_text("not a lock\n")
    victim.chmod(0o640)
    lock = tmp_path / "enclosure.lock"
    lock.symlink_to(victim)

    with enclosure_access(lock) as held:
        assert held is False, "a symlink was accepted as the enclosure lock"

    assert _mode(victim) == 0o640, f"the link target was chmod'ed to {_mode(victim):o}"
    assert victim.read_text() == "not a lock\n"
    assert lock.is_symlink(), "the refused entry must be left alone, not replaced"


def test_dangling_symlink_at_lock_path_creates_nothing(tmp_path: Path) -> None:
    """O_CREAT follows a dangling link and creates its target - as root, mode
    0666, anywhere root may write. That is a stronger primitive than the
    chmod: the attacker picks the path, then fills the world-writable file."""
    target = tmp_path / "created-by-root.pth"
    lock = tmp_path / "enclosure.lock"
    lock.symlink_to(target)

    with enclosure_access(lock) as held:
        assert held is False

    assert not target.exists(), "the open created the dangling link's target"


def test_hard_link_at_lock_path_is_refused_and_target_mode_unchanged(tmp_path: Path) -> None:
    """A second name for some other file is the same inode, so a chmod
    through it lands on that file. The lock is only ever a file of its own."""
    import os

    victim = tmp_path / "victim"
    victim.write_text("not a lock\n")
    victim.chmod(0o640)
    lock = tmp_path / "enclosure.lock"
    os.link(victim, lock)

    with enclosure_access(lock) as held:
        assert held is False, "a hard link to another file was accepted as the lock"

    assert _mode(victim) == 0o640, f"the linked file was chmod'ed to {_mode(victim):o}"


def test_fifo_at_lock_path_is_refused_and_not_chmodded(tmp_path: Path) -> None:
    """A FIFO must not be taken as the lock or chmod'ed.

    The time bound is only a guard against a future change of open mode:
    Linux never blocks an O_RDWR open of a FIFO, so this test cannot tell
    whether O_NONBLOCK is set. test_lock_is_opened_with_the_defensive_flags
    pins that flag instead."""
    import os

    lock = tmp_path / "enclosure.lock"
    os.mkfifo(lock, 0o600)

    started = time.monotonic()
    with enclosure_access(lock) as held:
        assert held is False, "a FIFO was accepted as the enclosure lock"
    assert time.monotonic() - started < 5

    assert _mode(lock) == 0o600, f"the FIFO was chmod'ed to {_mode(lock):o}"


def test_socket_at_lock_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import socket

    # Relative bind: an absolute pytest path can exceed the 108-byte sun_path.
    monkeypatch.chdir(tmp_path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind("enclosure.lock")
        with (
            caplog.at_level(logging.WARNING, logger="ktnmgr.enclosure.access"),
            enclosure_access(tmp_path / "enclosure.lock") as held,
        ):
            assert held is False
    finally:
        sock.close()

    # Refused as a substitute (ERROR, named), not lumped in with an ordinary
    # failure to open (WARNING).
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("socket" in m for m in messages), messages


def test_a_refused_lock_degrades_the_caller_instead_of_failing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a caller sees: the helper's SES read still runs and returns its
    result, unsynchronised, exactly as for any other unusable lock - and the
    planted link's target is untouched."""
    from ktnmgr.enclosure import ses as ses_module

    victim = tmp_path / "victim"
    victim.write_text("not a lock\n")
    victim.chmod(0o640)
    lock = tmp_path / "enclosure.lock"
    lock.symlink_to(victim)

    class _Proc:
        returncode = 0
        stdout = "page text"
        stderr = ""

    monkeypatch.setattr(ses_module.subprocess, "run", lambda *_a, **_k: _Proc())
    runner = ses_module.SesRunner(binary="/usr/bin/sg_ses", lock_path=lock)

    assert runner.read_page("/dev/sg16", "join").stdout == "page text"
    assert _mode(victim) == 0o640


def test_refusal_is_logged_as_an_error_naming_the_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Tampering is a security event, not routine degradation: it is logged
    at ERROR, once per path, and says what was found there."""
    import logging

    lock = tmp_path / "enclosure.lock"
    lock.symlink_to(tmp_path / "elsewhere")

    with caplog.at_level(logging.WARNING, logger="ktnmgr.enclosure.access"):
        for _ in range(3):
            with enclosure_access(lock):
                pass

    records = [r for r in caplog.records if str(lock) in r.getMessage()]
    assert len(records) == 1, f"expected one log line, got {len(records)}"
    assert records[0].levelno == logging.ERROR
    assert "symlink" in records[0].getMessage()


def test_pre_existing_regular_lock_is_used_and_its_mode_repaired(tmp_path: Path) -> None:
    """The ordinary restart case: the file is already there. Its owner (this
    process) may have created it under a restrictive umask, and the chmod
    that makes it shareable must still happen."""
    lock = tmp_path / "enclosure.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)

    with enclosure_access(lock) as held:
        assert held is True

    assert _mode(lock) == 0o666


def test_pre_existing_lock_owned_by_another_uid_is_used_but_not_chmodded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root opening the web process's file (or the reverse) must still take
    the lock, and must not try to chmod a file it does not own: that file's
    mode is its creator's business, and in the container (no CAP_FOWNER)
    the chmod could only fail anyway."""
    import os

    lock = tmp_path / "enclosure.lock"
    lock.write_bytes(b"")
    lock.chmod(0o646)
    owner = os.stat(lock).st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)

    with enclosure_access(lock) as held:
        assert held is True

    assert _mode(lock) == 0o646, "a lock owned by another uid was chmod'ed"


def _refusal_lines(caplog: pytest.LogCaptureFixture, lock: Path) -> list[str]:
    import logging

    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.ERROR and str(lock) in r.getMessage()
    ]


def _plant(lock: Path, kind: str, victim: Path) -> None:
    """Replace whatever is at ``lock`` with one kind of substitute."""
    import os
    import shutil

    if lock.is_dir() and not lock.is_symlink():
        shutil.rmtree(lock)
    elif lock.exists() or lock.is_symlink():
        lock.unlink()
    if kind == "symlink":
        lock.symlink_to(victim)
    elif kind == "fifo":
        os.mkfifo(lock, 0o600)
    elif kind == "directory":
        lock.mkdir()
    elif kind == "hard link":
        os.link(victim, lock)


def test_each_kind_of_refusal_is_logged_but_bounded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A different kind of substitute is reported at once, but however often
    the web process swaps plants - it drives the helper's request rate - the
    log gets at most one line per kind per interval, not one per request."""
    import logging

    victim = tmp_path / "victim"
    victim.write_text("not a lock\n")
    lock = tmp_path / "enclosure.lock"
    kinds = ["symlink", "fifo", "directory", "hard link"]

    with caplog.at_level(logging.WARNING, logger="ktnmgr.enclosure.access"):
        for count, kind in enumerate(kinds, start=1):
            _plant(lock, kind, victim)
            for _ in range(3):
                with enclosure_access(lock) as held:
                    assert held is False
            assert len(_refusal_lines(caplog, lock)) == count, (
                f"{kind}: {_refusal_lines(caplog, lock)}"
            )

        for _ in range(25):  # a hostile swap loop
            for kind in kinds:
                _plant(lock, kind, victim)
                with enclosure_access(lock):
                    pass

    lines = _refusal_lines(caplog, lock)
    assert len(lines) == len(kinds), lines
    for phrase in ("is a symlink", "is not a regular file", "is a directory",
                   "has a link count other than 1"):
        assert any(phrase in line for line in lines), (phrase, lines)


def test_a_refusal_that_persists_is_logged_again_after_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import os

    from ktnmgr.enclosure import access

    monkeypatch.setattr(access, "_REFUSAL_LOG_INTERVAL", 0.0)
    lock = tmp_path / "enclosure.lock"
    lock.symlink_to(tmp_path / "elsewhere")

    with caplog.at_level(logging.WARNING, logger="ktnmgr.enclosure.access"):
        for _ in range(3):
            with enclosure_access(lock):
                pass

    lines = _refusal_lines(caplog, lock)
    assert len(lines) == 3, lines
    # Which process refused: only the root helper's refusal is the event.
    assert f"(euid {os.geteuid()})" in lines[0]


def test_parent_symlink_loop_is_refused_and_named_as_a_loop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ELOOP also comes from a looping directory above the lock. That is a
    misconfigured path, not a planted link, and the message says so."""
    import logging

    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    lock = loop / "enclosure.lock"

    with (
        caplog.at_level(logging.WARNING, logger="ktnmgr.enclosure.access"),
        enclosure_access(lock) as held,
    ):
        assert held is False

    lines = _refusal_lines(caplog, lock)
    assert len(lines) == 1 and "symlink loop" in lines[0], lines


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
def test_refusals_do_not_leak_descriptors(tmp_path: Path) -> None:
    """The root helper hits the refusal path on every poll and every IDENT
    while a plant is in place. One leaked descriptor per refusal would
    exhaust its fd limit in hours and take IDENT and all SES telemetry down
    with it - turning a harmless refusal into the outage it exists to avoid.
    The FIFO and the hard link are the cases where a descriptor was opened
    before the refusal, so it must be closed."""
    import os

    victim = tmp_path / "victim"
    victim.write_text("not a lock\n")
    fifo = tmp_path / "fifo.lock"
    os.mkfifo(fifo, 0o600)
    linked = tmp_path / "linked.lock"
    os.link(victim, linked)
    unusable = tmp_path / "no-such-dir" / "enclosure.lock"

    def open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    before = open_fds()
    for _ in range(50):
        for path in (fifo, linked, unusable):
            with enclosure_access(path) as held:
                assert held is False
    assert open_fds() == before, f"{open_fds() - before} descriptors leaked"


def test_lock_is_opened_with_the_defensive_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the flags no behavioural test here can see. O_NOFOLLOW is also
    covered by the symlink tests; O_NONBLOCK and O_NOCTTY only matter for a
    device node (or a FIFO on a non-Linux kernel), which an unprivileged test
    cannot create."""
    import os

    seen: list[int] = []
    real_open = os.open

    def spy(path: object, flags: int, *args: object, **kwargs: object) -> int:
        seen.append(flags)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", spy)
    with enclosure_access(tmp_path / "enclosure.lock") as held:
        assert held is True
    monkeypatch.undo()

    assert seen, "the lock was not opened through os.open"
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC", "O_CREAT", "O_RDWR"):
        assert seen[0] & getattr(os, name) == getattr(os, name), f"{name} missing"


@pytest.mark.parametrize("plant", ["symlink", "unusable"])
def test_a_caller_exception_is_not_chained_to_the_open_failure(
    tmp_path: Path, plant: str
) -> None:
    """While a plant is in place, an unrelated error in the caller's block
    must not carry "is a symlink" in its traceback as its __context__."""
    if plant == "symlink":
        lock = tmp_path / "enclosure.lock"
        lock.symlink_to(tmp_path / "elsewhere")
    else:
        lock = tmp_path / "no-such-dir" / "enclosure.lock"

    with pytest.raises(RuntimeError) as info, enclosure_access(lock) as held:
        assert held is False
        raise RuntimeError("caller's own failure")

    assert info.value.__context__ is None, repr(info.value.__context__)


def test_settings_lock_path_prefers_the_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both processes must resolve the same file or they exclude nothing."""
    from ktnmgr.config import Settings

    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.enclosure_lock_path == tmp_path / "enclosure.lock"

    overridden = Settings(
        _env_file=None, data_dir=tmp_path, enclosure_lock=Path("/run/ktn/enclosure.lock")
    )
    assert overridden.enclosure_lock_path == Path("/run/ktn/enclosure.lock")
