"""Concurrent writes to the account file (the account-file race).

The account routes are plain ``def`` endpoints, so FastAPI runs them on a
threadpool and two requests really do execute at once. Every account write is
a read-modify-write of one JSON file, and none of them used to be serialised:

* two first-run bootstraps both passed the "no accounts yet" check, both were
  told ``{"ok": true}``, and the second write silently replaced the first
  administrator;
* concurrent revocations or a password change racing a revocation lost
  updates - including, measured below, a password change being undone;
* a login that rehashed an old-parameter hash wrote back the account list it
  had read *before* a concurrent password change, restoring the old password
  and the old session epoch;
* every writer shared one ``users.tmp``, truncating each other's half-written
  copy.

The bootstrap and password-change tests widen the race window with a hook on
the hasher, because Argon2 is exactly where the real window is: it runs for
tens of milliseconds between the read and the write. The hooks wait with a
timeout, never indefinitely, so an implementation that hashes while holding
the lock is slower here but cannot hang the suite. No test sleeps to "let a
race happen".
"""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from ktnmgr.services.auth import AuthError, AuthService

PASSWORD = "test-fixture-password"
NEW_PASSWORD = "a-different-password"

#: Upper bound for any single wait in this module. Generous so a loaded CI
#: runner does not flake; nothing waits this long on correct code except
#: where noted.
JOIN_TIMEOUT = 20.0

#: How long a hook holds a thread back waiting for the others. On the
#: unfixed code every party arrives at once; on a lock-holding implementation
#: this is how long one test pays before the hook gives up.
HOOK_TIMEOUT = 0.5

#: Every file the auth service is allowed to leave in its directory.
EXPECTED_FILES = {"users.json", "session-secret"}


def fast_hasher(time_cost: int = 1) -> PasswordHasher:
    """Argon2 at minimal cost: these tests are about ordering, not hashing."""
    return PasswordHasher(time_cost=time_cost, memory_cost=8, parallelism=1)


def make_auth(tmp_path: Path) -> AuthService:
    auth = AuthService(
        users_path=tmp_path / "users.json",
        secret_path=tmp_path / "session-secret",
        session_secret=None,
        max_age_seconds=3600,
        rate_limit=5,
        rate_window=60,
    )
    auth.hasher = fast_hasher()
    return auth


def accounts_on_disk(tmp_path: Path) -> dict[str, dict[str, object]]:
    data = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def epoch_on_disk(tmp_path: Path, username: str) -> int:
    return int(accounts_on_disk(tmp_path)[username]["session_epoch"])  # type: ignore[call-overload]


def password_works(auth: AuthService, username: str, password: str) -> bool:
    try:
        auth.verify(username, password)
    except AuthError:
        return False
    return True


def stray_files(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name not in EXPECTED_FILES)


class HookedHasher:
    """Wraps a PasswordHasher and runs ``before_hash`` ahead of each ``hash``.

    Only ``hash`` is intercepted; ``verify`` and ``check_needs_rehash`` pass
    straight through, so the hook fires exactly where a write is about to be
    computed.
    """

    def __init__(self, inner: PasswordHasher, before_hash: Callable[[], None]) -> None:
        self._inner = inner
        self._before_hash = before_hash

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        self._before_hash()
        return self._inner.hash(password, salt=salt)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def run_threads(targets: list[Callable[[], None]]) -> None:
    threads = [threading.Thread(target=target, daemon=True) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), "a worker thread hung"


# ----------------------------------------------------------------- bootstrap


@pytest.mark.parametrize("contenders", [2, 4])
def test_concurrent_bootstraps_create_exactly_one_administrator(
    tmp_path: Path, contenders: int
) -> None:
    """Exactly one first-run bootstrap succeeds; the rest are refused.

    The refused callers must get the same error a bootstrap gets today once an
    account exists, and the account on disk must be the one whose caller was
    told it succeeded. Before the fix every caller was told "created" and only
    the last writer's account survived.
    """
    auth = make_auth(tmp_path)
    # Every contender reaches the slow step (Argon2) before any of them moves
    # on - the widest version of the real window between check and write.
    rendezvous = threading.Barrier(contenders)

    def wait_for_everyone() -> None:
        try:
            rendezvous.wait(HOOK_TIMEOUT)
        except threading.BrokenBarrierError:
            pass

    auth.hasher = HookedHasher(fast_hasher(), wait_for_everyone)  # type: ignore[assignment]
    start = threading.Barrier(contenders)
    created: list[str] = []
    refused: list[str] = []
    unexpected: list[BaseException] = []
    guard = threading.Lock()

    def contender(name: str) -> Callable[[], None]:
        def run() -> None:
            start.wait(JOIN_TIMEOUT)
            try:
                auth.bootstrap(name, PASSWORD)
            except AuthError as exc:
                with guard:
                    refused.append(str(exc))
            except BaseException as exc:  # noqa: BLE001 - reported below
                with guard:
                    unexpected.append(exc)
            else:
                with guard:
                    created.append(name)

        return run

    run_threads([contender(f"admin{i}") for i in range(contenders)])

    assert not unexpected, f"unexpected errors: {unexpected!r}"
    assert len(created) == 1, f"{len(created)} callers were told they created the administrator"
    assert refused == ["an administrator account already exists"] * (contenders - 1)
    assert sorted(accounts_on_disk(tmp_path)) == created, "reported account is not the stored one"
    auth.hasher = fast_hasher()
    assert auth.verify(created[0], PASSWORD) == created[0]
    assert auth.needs_bootstrap is False
    assert stray_files(tmp_path) == []


def test_concurrent_bootstraps_without_a_hook(tmp_path: Path) -> None:
    """The same invariant with no hook at all, over repeated rounds.

    Independent of where the implementation hashes or locks: it only asserts
    the observable contract. Real Argon2 cost (the library default), because
    that is the window production has. Against the unfixed code four plain
    contenders put more than one "created" in 50 of 50 runs, so two rounds
    are plenty; with minimal-cost Argon2 it was 1 in 50, which is why the
    window is Argon2 and the hooked test above exists.
    """
    for round_ in range(2):
        directory = tmp_path / f"round{round_}"
        directory.mkdir()
        auth = make_auth(directory)
        auth.hasher = PasswordHasher()
        created = race_bootstraps(auth, contenders=4)
        assert len(created) == 1, f"round {round_}: {len(created)} callers told 'created'"
        assert sorted(accounts_on_disk(directory)) == created
        assert stray_files(directory) == []


def race_bootstraps(auth: AuthService, contenders: int) -> list[str]:
    """Start ``contenders`` bootstraps together; return the names told "created"."""
    start = threading.Barrier(contenders)
    created: list[str] = []
    guard = threading.Lock()

    def contender(name: str) -> Callable[[], None]:
        def run() -> None:
            start.wait(JOIN_TIMEOUT)
            try:
                auth.bootstrap(name, PASSWORD)
            except AuthError:
                return
            with guard:
                created.append(name)

        return run

    run_threads([contender(f"admin{i}") for i in range(contenders)])
    return created


# ------------------------------------------------- revoke and password change


def test_concurrent_revocations_lose_no_epoch_bumps(tmp_path: Path) -> None:
    """Every revocation must invalidate sessions: none may be lost.

    A lost bump is not cosmetic - a session issued between two revocations
    survives the one that was dropped.
    """
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    initial = epoch_on_disk(tmp_path, "admin")  # random, not 0: count from it
    workers, per_worker = 8, 25
    start = threading.Barrier(workers)
    unexpected: list[BaseException] = []
    guard = threading.Lock()

    def revoker() -> None:
        start.wait(JOIN_TIMEOUT)
        for _ in range(per_worker):
            try:
                auth.revoke_sessions("admin")
            except BaseException as exc:  # noqa: BLE001 - reported below
                with guard:
                    unexpected.append(exc)

    run_threads([revoker] * workers)

    assert not unexpected, f"{len(unexpected)} revocations failed: {unexpected[:3]!r}"
    assert epoch_on_disk(tmp_path, "admin") == initial + workers * per_worker
    assert stray_files(tmp_path) == []


def test_password_change_racing_revocations_keeps_both(tmp_path: Path) -> None:
    """A password change and revocations in flight together all take effect.

    Before the fix the change read the account list, then spent its Argon2
    time hashing, then wrote the list it had read - discarding every
    revocation that landed in between. The reverse order was worse: a
    revocation that read before the change wrote afterwards and put the OLD
    password hash back.
    """
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    initial = epoch_on_disk(tmp_path, "admin")  # random, not 0: count from it
    revokers = 6
    go_revoke = threading.Event()
    revokes_done = threading.Barrier(revokers + 1)
    change_thread: list[threading.Thread] = []

    def let_revocations_land() -> None:
        # Only the password change hashes here, and only once.
        if threading.current_thread() is not change_thread[0]:
            return
        go_revoke.set()
        try:
            revokes_done.wait(HOOK_TIMEOUT)
        except threading.BrokenBarrierError:
            pass

    auth.hasher = HookedHasher(fast_hasher(), let_revocations_land)  # type: ignore[assignment]
    unexpected: list[BaseException] = []
    guard = threading.Lock()

    def change() -> None:
        try:
            auth.change_password("admin", PASSWORD, NEW_PASSWORD)
        except BaseException as exc:  # noqa: BLE001 - reported below
            with guard:
                unexpected.append(exc)

    def revoke() -> None:
        go_revoke.wait(JOIN_TIMEOUT)
        try:
            auth.revoke_sessions("admin")
        except BaseException as exc:  # noqa: BLE001 - reported below
            with guard:
                unexpected.append(exc)
        try:
            revokes_done.wait(HOOK_TIMEOUT)
        except threading.BrokenBarrierError:
            pass

    changer = threading.Thread(target=change, daemon=True)
    change_thread.append(changer)
    others = [threading.Thread(target=revoke, daemon=True) for _ in range(revokers)]
    for thread in [*others, changer]:
        thread.start()
    for thread in [changer, *others]:
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), "a worker thread hung"

    assert not unexpected, f"unexpected errors: {unexpected!r}"
    auth.hasher = fast_hasher()
    assert password_works(auth, "admin", NEW_PASSWORD), "the password change was lost"
    assert not password_works(auth, "admin", PASSWORD), "the old password still works"
    assert epoch_on_disk(tmp_path, "admin") == initial + revokers + 1, "revocations were lost"
    assert stray_files(tmp_path) == []


def test_login_rehash_does_not_undo_a_concurrent_password_change(tmp_path: Path) -> None:
    """The opportunistic rehash on login must never write back stale state.

    A login with the old password, verified a moment before the password
    change, used to write the whole account list it had read - restoring the
    old password and the old session epoch, so the change that was meant to
    lock an intruder out silently let them back in.
    """
    auth = make_auth(tmp_path)
    # Created with weaker parameters than the service now uses, so the next
    # successful verify wants to rehash - the path under test.
    auth.hasher = fast_hasher(time_cost=1)
    auth.create_user("admin", PASSWORD)
    initial = epoch_on_disk(tmp_path, "admin")  # random, not 0: count from it
    current = fast_hasher(time_cost=2)
    login_thread: list[threading.Thread] = []
    change_errors: list[BaseException] = []

    def change_password_now() -> None:
        try:
            auth.change_password("admin", PASSWORD, NEW_PASSWORD)
        except BaseException as exc:  # noqa: BLE001 - reported below
            change_errors.append(exc)

    def password_changes_mid_login() -> None:
        # The login has verified the old password and is computing the new
        # hash. The administrator's password change lands right now.
        if threading.current_thread() is not login_thread[0]:
            return
        changer = threading.Thread(target=change_password_now, daemon=True)
        changer.start()
        changer.join(HOOK_TIMEOUT)
        login_thread.append(changer)

    auth.hasher = HookedHasher(current, password_changes_mid_login)  # type: ignore[assignment]
    login_result: list[object] = []

    def login() -> None:
        try:
            login_result.append(auth.verify("admin", PASSWORD))
        except BaseException as exc:  # noqa: BLE001 - reported below
            login_result.append(exc)

    login_thread.append(threading.Thread(target=login, daemon=True))
    login_thread[0].start()
    login_thread[0].join(JOIN_TIMEOUT)
    assert not login_thread[0].is_alive(), "login hung"
    assert len(login_thread) == 2, "the hook never fired: no rehash was attempted"
    login_thread[1].join(JOIN_TIMEOUT)
    assert not login_thread[1].is_alive(), "password change hung"

    assert login_result == ["admin"], "the login verified before the change should succeed"
    assert not change_errors, f"password change failed: {change_errors!r}"
    auth.hasher = current
    assert password_works(auth, "admin", NEW_PASSWORD), "the password change was undone"
    assert not password_works(auth, "admin", PASSWORD), "the old password works again"
    assert epoch_on_disk(tmp_path, "admin") == initial + 1, "the session revocation was undone"
    assert stray_files(tmp_path) == []


def test_login_rehash_still_upgrades_an_old_hash(tmp_path: Path) -> None:
    """The guard against stale writes must not disable the rehash itself."""
    auth = make_auth(tmp_path)
    auth.hasher = fast_hasher(time_cost=1)
    auth.create_user("admin", PASSWORD)
    initial = epoch_on_disk(tmp_path, "admin")  # random, not 0: count from it
    auth.hasher = fast_hasher(time_cost=2)
    before = accounts_on_disk(tmp_path)["admin"]["password_hash"]
    assert auth.hasher.check_needs_rehash(str(before))

    assert auth.verify("admin", PASSWORD) == "admin"

    after = str(accounts_on_disk(tmp_path)["admin"]["password_hash"])
    assert after != before
    assert not auth.hasher.check_needs_rehash(after)
    assert epoch_on_disk(tmp_path, "admin") == initial, "a rehash must not sign anyone out"
    assert stray_files(tmp_path) == []


# --------------------------------------------------------------- the write


def test_services_on_one_account_file_share_one_lock(tmp_path: Path) -> None:
    """The lock belongs to the file, not to the AuthService object.

    Two services pointed at the same file - the tests build several, and
    nothing stops the app doing so - must exclude each other, including when
    the path is spelled through a symlink; services on different files must
    not contend.
    """
    first = make_auth(tmp_path)
    second = make_auth(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    via_link = make_auth(link)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = make_auth(other_dir)

    assert first._accounts_lock is second._accounts_lock
    assert first._accounts_lock is via_link._accounts_lock
    assert first._accounts_lock is not other._accounts_lock


def test_writers_outside_the_lock_never_tear_the_account_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writes in flight at once still each land whole.

    Inside this process the lock stops two writes overlapping at all. A
    second process writing the same file does not share that lock - not a
    supported configuration, but nothing stops an operator starting one - and
    this is what it gets: last writer wins, but the file is always one
    complete version. With the old shared ``users.tmp`` both writers wrote
    into one file, the second rename failed, and the live file could be
    invalid JSON, which fails closed and locks every account out.

    ``_write_users`` is called directly from two threads to stand in for the
    two processes. Both are held after writing and before the rename, so the
    overlap is certain rather than lucky.
    """
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    real_replace = os.replace
    both_written = threading.Barrier(2)

    def replace_once_both_have_written(
        src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object
    ) -> None:
        try:
            both_written.wait(HOOK_TIMEOUT)
        except threading.BrokenBarrierError:
            pass
        real_replace(src, dst)

    # Different lengths, so an interleaving of the two would not parse.
    short: dict[str, dict[str, object]] = {"admin": {"password_hash": "x", "session_epoch": 1}}
    long: dict[str, dict[str, object]] = {
        "admin": {"password_hash": "y" * 4096, "session_epoch": 2}
    }
    errors: list[BaseException] = []

    def writer(users: dict[str, dict[str, object]]) -> Callable[[], None]:
        def run() -> None:
            try:
                auth._write_users(users)  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        return run

    monkeypatch.setattr(os, "replace", replace_once_both_have_written)
    run_threads([writer(short), writer(long)])
    monkeypatch.undo()

    assert not errors, f"a write failed: {errors!r}"
    assert accounts_on_disk(tmp_path) in (short, long), "the file is not one whole version"
    assert stray_files(tmp_path) == []


def test_a_failed_replace_leaves_the_old_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails part-way must not leave a copy of the hashes behind
    or damage the account file it was replacing."""
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    initial = epoch_on_disk(tmp_path, "admin")  # random, not 0: count from it
    original = (tmp_path / "users.json").read_bytes()

    def refuse(src: object, dst: object, **kwargs: object) -> None:
        raise OSError("simulated: disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        auth.revoke_sessions("admin")
    monkeypatch.undo()

    assert (tmp_path / "users.json").read_bytes() == original
    assert stray_files(tmp_path) == []
    # And the next write still works: nothing was left locked.
    auth.revoke_sessions("admin")
    assert epoch_on_disk(tmp_path, "admin") == initial + 1


def test_account_file_is_flushed_to_disk_before_it_is_swapped_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync before the rename, so a power cut cannot leave an empty or
    truncated account file - which would fail closed and lock everyone out.

    Also checks the copy being swapped in is 0600 at that moment even under a
    permissive umask: it holds the same hashes as the real file.
    """
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(
        src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object
    ) -> None:
        events.append(f"replace mode={stat.S_IMODE(os.stat(src).st_mode):o}")
        real_replace(src, dst)

    old_umask = os.umask(0)
    try:
        monkeypatch.setattr(os, "fsync", recording_fsync)
        monkeypatch.setattr(os, "replace", recording_replace)
        auth.revoke_sessions("admin")
    finally:
        os.umask(old_umask)
        monkeypatch.undo()

    replaces = [i for i, e in enumerate(events) if e.startswith("replace")]
    assert len(replaces) == 1, events
    assert "fsync" in events[: replaces[0]], f"no fsync before the rename: {events}"
    assert events[replaces[0]] == "replace mode=600", events
    assert stat.S_IMODE((tmp_path / "users.json").stat().st_mode) == 0o600
    assert stray_files(tmp_path) == []


def test_account_file_rename_is_made_durable_with_a_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the rename, the directory itself is fsync'ed: without it a power
    cut shortly after can bring back the previous account file - the previous
    password and session epoch."""
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def recording_fsync(fd: int) -> None:
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
        real_fsync(fd)

    def recording_replace(
        src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object
    ) -> None:
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)
    auth.revoke_sessions("admin")
    monkeypatch.undo()

    assert events == ["fsync-file", "replace", "fsync-dir"], events


@pytest.mark.parametrize(
    ("err", "warned"),
    [(errno.EIO, True), (errno.EINVAL, False), (errno.EOPNOTSUPP, False)],
)
def test_a_failed_directory_fsync_never_fails_the_write_and_a_real_fault_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    err: int, warned: bool,
) -> None:
    """Best effort: the new file is already in place and whole, so the write
    succeeds whatever the directory fsync says. A filesystem that simply does
    not fsync directories (EINVAL, EOPNOTSUPP) stays at debug; a storage fault
    (EIO) is logged as a warning, because it is one an operator should see."""
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    initial = epoch_on_disk(tmp_path, "admin")  # random, not 0: count from it
    real_fsync = os.fsync

    def failing_dir_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(err, os.strerror(err))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_dir_fsync)
    with caplog.at_level("DEBUG", logger="ktnmgr.services.auth"):
        auth.revoke_sessions("admin")
    monkeypatch.undo()

    assert epoch_on_disk(tmp_path, "admin") == initial + 1, "the write must still land"
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "fsync" in r.message]
    assert bool(warnings) is warned, [(r.levelname, r.message) for r in caplog.records]
    assert any("fsync" in r.message for r in caplog.records), "control: the failure was seen"
    assert stray_files(tmp_path) == []


# ----------------------------------------- change_password's verify window


class VerifyHookedHasher:
    """Like HookedHasher, but runs ``before_verify`` ahead of each ``verify``."""

    def __init__(self, inner: PasswordHasher, before_verify: Callable[[], None]) -> None:
        self._inner = inner
        self._before_verify = before_verify

    def verify(self, hash_: str, password: str) -> bool:
        self._before_verify()
        return self._inner.verify(hash_, password)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_revocations_during_a_password_changes_verify_are_kept(tmp_path: Path) -> None:
    """change_password writes back the account list it read before its Argon2
    VERIFY, so the lock must cover the verify too, not only the new hash.
    Shrinking it to "just the write" - an obvious-looking optimisation - would
    lose every revocation that lands during the verify, which the hash-window
    test above cannot see. Here the revocations are released from inside the
    password change's verify."""
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    initial = epoch_on_disk(tmp_path, "admin")  # random, not 0: count from it
    revokers = 6
    released = threading.Event()
    all_back = threading.Barrier(revokers + 1)
    changer: list[int] = []  # thread ident of the password change
    errors: list[BaseException] = []

    def hold_the_verify() -> None:
        if threading.get_ident() in changer:
            released.set()
            try:
                all_back.wait(HOOK_TIMEOUT)
            except threading.BrokenBarrierError:
                pass  # correct code: the revokers are queued behind the lock

    auth.hasher = VerifyHookedHasher(fast_hasher(), hold_the_verify)  # type: ignore[assignment]

    def change() -> None:
        changer.append(threading.get_ident())
        try:
            auth.change_password("admin", PASSWORD, NEW_PASSWORD)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def revoke() -> None:
        released.wait(JOIN_TIMEOUT)
        try:
            auth.revoke_sessions("admin")
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        try:
            all_back.wait(HOOK_TIMEOUT)
        except threading.BrokenBarrierError:
            pass

    run_threads([*[revoke for _ in range(revokers)], change])

    assert not errors, f"{errors!r}"
    auth.hasher = fast_hasher()
    assert password_works(auth, "admin", NEW_PASSWORD), "the password change was lost"
    assert epoch_on_disk(tmp_path, "admin") == initial + revokers + 1, (
        "revocations that landed during the change's verify were lost"
    )
    assert stray_files(tmp_path) == []


# ------------------------------------------------- leftover temp files


#: Old enough for the startup sweep (its threshold is 60 s).
STALE = 3600.0


def age(path: Path, seconds: float) -> Path:
    """Backdate ``path`` (itself, never a symlink's target)."""
    when = time.time() - seconds
    os.utime(path, (when, when), follow_symlinks=False)
    return path


def plant(tmp_path: Path, name: str, seconds: float = STALE) -> Path:
    path = tmp_path / name
    path.write_text('{"admin": {"password_hash": "stale"}}', encoding="utf-8")
    os.chmod(path, 0o600)
    return age(path, seconds)


def test_startup_removes_stale_account_temp_files(tmp_path: Path) -> None:
    """A crash between creating the temp file and renaming it leaves a copy of
    the password hashes that nothing reads and nothing removes - including a
    hash changed because it leaked. Releases before the unique temp file left
    a fixed-name users.tmp. Both go at startup once they are stale."""
    make_auth(tmp_path).create_user("admin", PASSWORD)
    plant(tmp_path, "users.json.k3x_9q2a.tmp")
    plant(tmp_path, "users.json.abcdefgh.tmp")
    plant(tmp_path, "users.tmp")
    assert len(stray_files(tmp_path)) == 3, "control: the leftovers are there"

    auth = make_auth(tmp_path)

    assert stray_files(tmp_path) == []
    assert password_works(auth, "admin", PASSWORD), "the real account file is untouched"


def test_startup_keeps_a_temp_file_that_may_still_be_in_use(tmp_path: Path) -> None:
    """Younger than the threshold: possibly another writer's in-flight file.
    Removing it would make that writer's rename fail."""
    plant(tmp_path, "users.json.k3x_9q2a.tmp", seconds=5)
    plant(tmp_path, "users.tmp", seconds=5)
    make_auth(tmp_path)
    assert stray_files(tmp_path) == ["users.json.k3x_9q2a.tmp", "users.tmp"]


@pytest.mark.parametrize(
    "name",
    [
        "users.json.tmp",  # no random part
        "users.json.abcdefg.tmp",  # 7 random characters
        "users.json.abcdefghi.tmp",  # 9
        "users.json.ABCDEFGH.tmp",  # mkstemp never uses capitals
        "users.json.abcd-fgh.tmp",  # nor hyphens
        "users.json.abcdefgh.tmp.bak",
        "xusers.json.abcdefgh.tmp",
        "other.json.abcdefgh.tmp",
        "users.tmp.bak",
        "users.json.bak",
        "notes.txt",
    ],
)
def test_startup_never_touches_a_name_that_is_not_exactly_a_temp_file(
    tmp_path: Path, name: str
) -> None:
    plant(tmp_path, name)
    make_auth(tmp_path)
    assert (tmp_path / name).exists(), f"{name} was removed"


def test_startup_never_follows_or_removes_a_symlink(tmp_path: Path) -> None:
    """A symlink named like a temp file is left alone, and so is its target -
    the sweep must not become a way to delete files elsewhere."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = plant(elsewhere, "precious")
    data = tmp_path / "data"
    data.mkdir()
    link = data / "users.json.abcdefgh.tmp"
    link.symlink_to(target)
    age(link, STALE)
    legacy_link = data / "users.tmp"
    legacy_link.symlink_to(target)
    age(legacy_link, STALE)
    # Control: a regular stale temp file beside them IS removed, so the sweep ran.
    plant(data, "users.json.k3x_9q2a.tmp")

    make_auth(data)

    assert not (data / "users.json.k3x_9q2a.tmp").exists(), "control: the sweep did not run"
    assert link.is_symlink() and legacy_link.is_symlink()
    assert target.read_text(encoding="utf-8").startswith("{"), "the link's target was touched"


def test_startup_never_removes_a_directory_named_like_a_temp_file(tmp_path: Path) -> None:
    folder = tmp_path / "users.json.abcdefgh.tmp"
    folder.mkdir()
    age(folder, STALE)
    make_auth(tmp_path)
    assert folder.is_dir()


def test_startup_survives_a_temp_file_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    plant(tmp_path, "users.json.abcdefgh.tmp")

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "unlink", refuse)
    with caplog.at_level("WARNING", logger="ktnmgr.services.auth"):
        auth = make_auth(tmp_path)
    monkeypatch.undo()

    assert auth.needs_bootstrap, "the service still came up"
    assert any("stale account temp file" in r.message for r in caplog.records)


def test_startup_with_no_data_directory_yet_is_fine(tmp_path: Path) -> None:
    auth = AuthService(
        users_path=tmp_path / "missing" / "users.json",
        secret_path=tmp_path / "secret",
        session_secret="fixed-for-the-test",
        max_age_seconds=3600,
        rate_limit=5,
        rate_window=60,
    )
    assert auth.needs_bootstrap


def test_the_sweep_pattern_matches_the_temp_files_writes_really_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the sweep to what tempfile.mkstemp actually names files on this
    Python. If a future version changed the random part, the sweep would
    quietly match nothing; this test makes that loud instead."""
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    seen: list[str] = []
    real_replace = os.replace

    def keep_the_temp_file(
        src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object
    ) -> None:
        seen.append(Path(src).name)
        raise OSError(errno.EIO, "simulated crash before the rename")

    for _ in range(5):
        monkeypatch.setattr(os, "replace", keep_the_temp_file)
        monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: None)
        with pytest.raises(OSError):
            auth.revoke_sessions("admin")
        monkeypatch.undo()
    assert os.replace is real_replace
    assert len(seen) == 5 and sorted(stray_files(tmp_path)) == sorted(seen)
    for name in seen:
        age(tmp_path / name, STALE)

    make_auth(tmp_path)

    assert stray_files(tmp_path) == [], f"the sweep missed real temp names: {seen}"
