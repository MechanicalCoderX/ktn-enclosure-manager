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

import json
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from ktnmgr.services.auth import AuthError, AuthService

PASSWORD = "the-original-password"
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
    assert epoch_on_disk(tmp_path, "admin") == workers * per_worker
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
    assert epoch_on_disk(tmp_path, "admin") == revokers + 1, "revocations were lost"
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
    assert epoch_on_disk(tmp_path, "admin") == 1, "the session revocation was undone"
    assert stray_files(tmp_path) == []


def test_login_rehash_still_upgrades_an_old_hash(tmp_path: Path) -> None:
    """The guard against stale writes must not disable the rehash itself."""
    auth = make_auth(tmp_path)
    auth.hasher = fast_hasher(time_cost=1)
    auth.create_user("admin", PASSWORD)
    auth.hasher = fast_hasher(time_cost=2)
    before = accounts_on_disk(tmp_path)["admin"]["password_hash"]
    assert auth.hasher.check_needs_rehash(str(before))

    assert auth.verify("admin", PASSWORD) == "admin"

    after = str(accounts_on_disk(tmp_path)["admin"]["password_hash"])
    assert after != before
    assert not auth.hasher.check_needs_rehash(after)
    assert epoch_on_disk(tmp_path, "admin") == 0, "a rehash must not sign anyone out"
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
    assert epoch_on_disk(tmp_path, "admin") == 1


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
