"""create_user() refuses an existing username instead of replacing the account.

Replacing it reset ``session_epoch`` (to 0, before new accounts started at a
random epoch), which brought back to life every session cookie for that name
carrying the reset value and still inside its max age - including
ones that a password change or "sign out everywhere" had ended. No route calls
create_user() today (first-run setup goes through bootstrap(), which already
refuses once any account exists), so this closes a trap for future callers
rather than a reachable hole. Found by the 1.6.3 hardening review.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from ktnmgr.services.auth import (
    INITIAL_EPOCH_FLOOR,
    INITIAL_EPOCH_RANDOM_BITS,
    AuthError,
    AuthService,
)

PASSWORD = "correct horse battery staple"
OTHER = "a different long password"


def make_auth(tmp_path: Path) -> AuthService:
    return AuthService(
        users_path=tmp_path / "users.json",
        secret_path=tmp_path / "session-secret",
        session_secret=None,
        max_age_seconds=3600,
        rate_limit=5,
        rate_window=60,
    )


def test_existing_username_is_refused_and_the_account_is_unchanged(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    before = (tmp_path / "users.json").read_bytes()

    with pytest.raises(AuthError, match="already exists"):
        auth.create_user("admin", OTHER)

    assert (tmp_path / "users.json").read_bytes() == before
    auth.verify("admin", PASSWORD)
    with pytest.raises(AuthError):
        auth.verify("admin", OTHER)


def test_refusal_does_not_revive_sessions_ended_by_a_revocation(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    initial = json.loads((tmp_path / "users.json").read_text())["admin"]["session_epoch"]
    old_token = auth.issue_session("admin")  # at the initial epoch
    auth.revoke_sessions("admin")  # initial + 1: old_token is dead
    assert auth.read_session(old_token) is None

    with pytest.raises(AuthError):
        auth.create_user("admin", OTHER)

    assert auth.read_session(old_token) is None
    record = json.loads((tmp_path / "users.json").read_text())["admin"]
    assert record["session_epoch"] == initial + 1


def test_a_new_username_is_still_created_next_to_an_existing_one(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    auth.create_user("ops", OTHER)

    auth.verify("admin", PASSWORD)
    auth.verify("ops", OTHER)


# ------------------------------------------------------------------ the race
#
# The check above is only worth anything if it runs under the account lock,
# against the file as it is at write time. Read the account list outside the
# lock - before the Argon2 hash, which is tens of milliseconds wide, or just
# after it - and two concurrent create_user() calls for one name can both see
# "no such account" and both write: the second replaces the first and resets
# its epoch, which is exactly what the refusal exists to stop. The
# single-threaded tests above cannot tell the orderings apart (REVIEW-164 F5:
# that mutant passed all 81 auth tests).

#: Rounds per race test. Each round is a fresh account file.
HOOKED_ROUNDS = 5
REAL_ARGON2_ROUNDS = 3

#: Upper bound for any single wait. Nothing waits this long on correct code.
JOIN_TIMEOUT = 20.0

#: How long the read hook holds a reader back waiting for the other caller's
#: read. Under the lock the other caller cannot read until this one has
#: written, so correct code pays this once per round and carries on; a read
#: made outside the lock meets the other caller's read at once.
HOOK_TIMEOUT = 0.25


def fast_hasher() -> PasswordHasher:
    """Argon2 at minimal cost: the hooked race is about ordering, not hashing."""
    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def hold_reads_until_both_have_read(auth: AuthService) -> None:
    """After each read of the account list, wait (bounded) for the other caller's read.

    The widest form of the race, made deterministic: if both callers can
    read before either writes, they do, on every round. Correct code reads
    under the lock, so the second read cannot happen until the first caller
    has written; its wait times out and the second caller sees that write.
    """
    rendezvous = threading.Barrier(2)
    read_users = auth._read_users

    def read_then_wait() -> dict[str, Any]:
        users = read_users()
        try:
            rendezvous.wait(HOOK_TIMEOUT)
        except threading.BrokenBarrierError:
            pass
        return users

    auth._read_users = read_then_wait  # type: ignore[method-assign]


def race_create_admin(auth: AuthService) -> tuple[list[str], list[str]]:
    """Two threads call create_user("admin") together, each with its own password.

    Returns (passwords that were told "created", refusal messages).
    """
    start = threading.Barrier(2)
    created: list[str] = []
    refused: list[str] = []
    unexpected: list[BaseException] = []
    guard = threading.Lock()

    def contender(password: str) -> None:
        start.wait(JOIN_TIMEOUT)
        try:
            auth.create_user("admin", password)
        except AuthError as exc:
            with guard:
                refused.append(str(exc))
        except BaseException as exc:  # noqa: BLE001 - reported below
            with guard:
                unexpected.append(exc)
        else:
            with guard:
                created.append(password)

    threads = [
        threading.Thread(target=contender, args=(pw,), daemon=True) for pw in (PASSWORD, OTHER)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), "a create_user thread hung"
    assert not unexpected, f"unexpected errors: {unexpected!r}"
    return created, refused


def assert_exactly_one_admin(
    directory: Path, created: list[str], refused: list[str], label: str, hasher: PasswordHasher
) -> None:
    assert len(created) == 1, f"{label}: {len(created)} of 2 concurrent creates succeeded"
    assert refused == ["an account named 'admin' already exists"], f"{label}: {refused!r}"
    accounts = json.loads((directory / "users.json").read_text())
    assert sorted(accounts) == ["admin", "ops"], f"{label}: {sorted(accounts)!r}"
    # A fresh initial epoch: never bumped, never reset to 0.
    epoch = accounts["admin"]["session_epoch"]
    assert INITIAL_EPOCH_FLOOR <= epoch < INITIAL_EPOCH_FLOOR + 2**INITIAL_EPOCH_RANDOM_BITS
    # The stored account is the one whose caller was told it was created.
    # Same Argon2 parameters as the writer, so verify() has no rehash to do.
    checker = make_auth(directory)
    checker.hasher = hasher
    checker.verify("admin", created[0])
    loser = OTHER if created[0] == PASSWORD else PASSWORD
    with pytest.raises(AuthError):
        checker.verify("admin", loser)


def test_concurrent_creates_of_one_name_create_exactly_one(tmp_path: Path) -> None:
    """Two concurrent create_user("admin"): exactly one succeeds, every round.

    The read hook lets both callers read before either writes whenever the
    implementation allows it, so an existence check made against a read
    outside the lock lets both through on every round, not on unlucky timing.
    """
    for round_ in range(HOOKED_ROUNDS):
        directory = tmp_path / f"round{round_}"
        directory.mkdir()
        auth = make_auth(directory)
        auth.hasher = fast_hasher()
        auth.create_user("ops", PASSWORD)  # the file exists and is not empty
        hold_reads_until_both_have_read(auth)

        created, refused = race_create_admin(auth)

        assert_exactly_one_admin(directory, created, refused, f"round {round_}", fast_hasher())


def test_concurrent_creates_of_one_name_without_a_hook(tmp_path: Path) -> None:
    """The same contract with no hook, at the library's default Argon2 cost.

    Independent of how the implementation reads or locks: only the observable
    result is asserted. The default-cost hash is the window production has;
    the reviewer's probe put both callers through on 40 of 40 rounds when the
    list was read before the hash.
    """
    for round_ in range(REAL_ARGON2_ROUNDS):
        directory = tmp_path / f"round{round_}"
        directory.mkdir()
        auth = make_auth(directory)
        auth.hasher = fast_hasher()
        auth.create_user("ops", PASSWORD)
        auth.hasher = PasswordHasher()

        created, refused = race_create_admin(auth)

        assert_exactly_one_admin(
            directory, created, refused, f"round {round_}", PasswordHasher()
        )
