"""Auth failure modes that must fail closed.

The bug these guard: an unreadable or corrupt account file used to be
indistinguishable from "no accounts yet", which reopened the unauthenticated
bootstrap endpoint and let anyone on the network claim an administrator
account - then overwrite the real one on first write.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from ktnmgr.services.auth import AuthError, AuthService


def make_auth(tmp_path: Path) -> AuthService:
    return AuthService(
        users_path=tmp_path / "users.json",
        secret_path=tmp_path / "session-secret",
        session_secret=None,
        max_age_seconds=3600,
        rate_limit=5,
        rate_window=60,
    )


def test_absent_account_file_allows_bootstrap(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    assert auth.needs_bootstrap is True
    auth.bootstrap("admin", "correct horse battery")
    assert auth.needs_bootstrap is False


def test_corrupt_account_file_does_not_reopen_bootstrap(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", "correct horse battery")

    (tmp_path / "users.json").write_text("{ this is not json")

    assert auth.needs_bootstrap is False, "corrupt file reopened bootstrap"
    with pytest.raises(AuthError):
        auth.bootstrap("attacker", "attacker password 1")


def test_account_file_of_wrong_shape_fails_closed(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    (tmp_path / "users.json").write_text(json.dumps(["not", "a", "dict"]))

    assert auth.needs_bootstrap is False
    with pytest.raises(AuthError):
        auth.bootstrap("attacker", "attacker password 1")


def test_unreadable_account_file_rejects_sessions(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", "correct horse battery")
    token = auth.issue_session("admin")
    assert auth.read_session(token) == "admin"

    (tmp_path / "users.json").write_text("{ corrupt")
    assert auth.read_session(token) is None, "session accepted without confirming the account"


def test_secret_and_account_files_are_never_world_readable(tmp_path: Path) -> None:
    """Written 0600 from creation, not chmod'ed afterwards."""
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", "correct horse battery")

    for name in ("users.json", "session-secret"):
        mode = (tmp_path / name).stat().st_mode
        assert not mode & stat.S_IRGRP, f"{name} is group readable"
        assert not mode & stat.S_IROTH, f"{name} is world readable"
        assert stat.S_IMODE(mode) == 0o600, f"{name} mode is {stat.S_IMODE(mode):o}"


def test_temp_file_left_behind_is_also_private(tmp_path: Path) -> None:
    """The atomic-replace temp file holds the same hashes as the real one."""
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", "correct horse battery")
    leftovers = list(tmp_path.glob("*.tmp"))
    for leftover in leftovers:
        assert stat.S_IMODE(leftover.stat().st_mode) == 0o600
