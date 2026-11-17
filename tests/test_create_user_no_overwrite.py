"""create_user() refuses an existing username instead of replacing the account.

Replacing it reset ``session_epoch`` to 0, which brought back to life every
epoch-0 session cookie for that name still inside its max age - including
ones that a password change or "sign out everywhere" had ended. No route calls
create_user() today (first-run setup goes through bootstrap(), which already
refuses once any account exists), so this closes a trap for future callers
rather than a reachable hole. Found by the 1.6.3 hardening review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ktnmgr.services.auth import AuthError, AuthService

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
    old_token = auth.issue_session("admin")  # epoch 0
    auth.revoke_sessions("admin")  # epoch 1: old_token is dead
    assert auth.read_session(old_token) is None

    with pytest.raises(AuthError):
        auth.create_user("admin", OTHER)

    assert auth.read_session(old_token) is None
    record = json.loads((tmp_path / "users.json").read_text())["admin"]
    assert record["session_epoch"] == 1


def test_a_new_username_is_still_created_next_to_an_existing_one(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.create_user("admin", PASSWORD)
    auth.create_user("ops", OTHER)

    auth.verify("admin", PASSWORD)
    auth.verify("ops", OTHER)
