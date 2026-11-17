"""A new account starts at a random session epoch, never at 0.

Every account used to be created at ``session_epoch`` 0. Deleting users.json
is the only documented-by-default way to recover a lost password, and the
session secret survives it (it is a separate file, or KTN_SESSION_SECRET).
So after delete + bootstrap of the same username, every epoch-0 cookie for
that name still inside its max age was accepted again - including cookies
that "sign out everywhere" or a password change had ended (REVIEW-164 F1,
probe c1_epoch). A random starting epoch, drawn from a range no pre-existing
account can reach, closes that for bootstrap and create_user alike, and needs
no migration: accounts and cookies from older releases keep their epochs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app
from ktnmgr.services.auth import SESSION_COOKIE, AuthService

PASSWORD = "correct horse battery staple"
SECOND = "a second long password"
THIRD = "a third long password here"
CSRF = {"x-ktn-request": "1"}
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"

#: Initial epochs live in [2**52, 2**53): never 0 (the value a cookie without
#: an "e" field reads as), above anything a pre-existing account reached by
#: counting up from 0, and exactly representable as an IEEE double, so a JSON
#: tool that parses numbers as doubles (jq 1.6, JavaScript) cannot round one.
EPOCH_FLOOR = 2**52
EPOCH_CEILING = 2**53


def make_auth(tmp_path: Path) -> AuthService:
    return AuthService(
        users_path=tmp_path / "users.json",
        secret_path=tmp_path / "session-secret",
        session_secret=None,
        max_age_seconds=3600,
        rate_limit=5,
        rate_window=60,
    )


def accounts(tmp_path: Path) -> dict[str, dict[str, object]]:
    data = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def stored_epoch(tmp_path: Path, username: str) -> int:
    value = accounts(tmp_path)[username]["session_epoch"]
    assert isinstance(value, int)
    return value


def drop_account(tmp_path: Path, username: str) -> None:
    """What an operator does by hand: there is no delete-account API."""
    data = accounts(tmp_path)
    del data[username]
    (tmp_path / "users.json").write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------- the reviewer's probe (F1)


def test_delete_and_bootstrap_does_not_revive_a_revoked_cookie(tmp_path: Path) -> None:
    """The c1_epoch probe: users.json removed, session secret kept."""
    first = make_auth(tmp_path)
    first.bootstrap("admin", PASSWORD)
    _, old = first.login("admin", PASSWORD)
    first.revoke_sessions("admin")
    assert first.read_session(old) is None

    (tmp_path / "users.json").unlink()
    second = make_auth(tmp_path)  # a restart: same session-secret file
    second.bootstrap("admin", SECOND)

    assert second.read_session(old) is None
    # The new account itself works.
    _, fresh = second.login("admin", SECOND)
    assert second.read_session(fresh) == "admin"


def test_delete_and_bootstrap_does_not_revive_a_cookie_a_password_change_ended(
    tmp_path: Path,
) -> None:
    first = make_auth(tmp_path)
    first.bootstrap("admin", PASSWORD)
    _, old = first.login("admin", PASSWORD)
    first.change_password("admin", PASSWORD, SECOND)
    assert first.read_session(old) is None

    (tmp_path / "users.json").unlink()
    second = make_auth(tmp_path)
    second.bootstrap("admin", THIRD)

    assert second.read_session(old) is None


def test_delete_and_bootstrap_does_not_accept_the_old_accounts_live_cookie(
    tmp_path: Path,
) -> None:
    """A cookie the old account never revoked belongs to an account that no
    longer exists; it must not sign in to its namesake either."""
    first = make_auth(tmp_path)
    first.bootstrap("admin", PASSWORD)
    _, old = first.login("admin", PASSWORD)

    (tmp_path / "users.json").unlink()
    second = make_auth(tmp_path)
    second.bootstrap("admin", SECOND)

    assert second.read_session(old) is None


def test_create_user_after_removing_an_account_does_not_revive_its_cookies(
    tmp_path: Path,
) -> None:
    """The create_user path: an account removed by hand, then re-created."""
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", PASSWORD)
    auth.create_user("ops", PASSWORD)
    _, revoked = auth.login("ops", PASSWORD)
    auth.revoke_sessions("ops")
    _, live = auth.login("ops", PASSWORD)

    drop_account(tmp_path, "ops")
    auth.create_user("ops", SECOND)

    assert auth.read_session(revoked) is None
    assert auth.read_session(live) is None
    _, fresh = auth.login("ops", SECOND)
    assert auth.read_session(fresh) == "ops"


def test_delete_and_bootstrap_over_http_does_not_revive_a_revoked_cookie(
    tmp_path: Path,
) -> None:
    """End to end: the operator's real sequence, through the API, with the
    app rebuilt (a container restart) on the same data directory."""
    sysfs = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    data_dir = tmp_path / "data"

    def settings() -> Settings:
        return Settings(
            _env_file=None,
            sysfs_root=sysfs,
            dev_root=tmp_path / "dev",
            data_dir=data_dir,
            truenas_url="",
            poll_slots_seconds=0.1,
            ident_method="sysfs",
        )

    creds = {"username": "admin", "password": PASSWORD}
    with TestClient(build_app(settings())) as client:
        assert client.post("/api/auth/bootstrap", json=creds, headers=CSRF).status_code == 200
        assert client.post("/api/auth/login", json=creds, headers=CSRF).status_code == 200
        old = client.cookies.get(SESSION_COOKIE)
        assert old
        assert client.get("/api/enclosures").status_code == 200
        assert client.post("/api/auth/revoke-sessions", headers=CSRF).status_code == 200

    users = data_dir / "users.json"
    assert users.exists()
    users.unlink()

    new_creds = {"username": "admin", "password": SECOND}
    with TestClient(build_app(settings())) as client:
        assert (
            client.post("/api/auth/bootstrap", json=new_creds, headers=CSRF).status_code == 200
        )
        client.cookies.set(SESSION_COOKIE, old)
        assert client.get("/api/enclosures").status_code == 401


# ---------------------------------------------- legacy cookies (no "e" field)


@pytest.mark.parametrize("create", ["bootstrap", "create_user"])
def test_a_cookie_without_an_epoch_does_not_match_a_new_account(
    tmp_path: Path, create: str
) -> None:
    """A cookie from before the epoch field reads as epoch 0; a new account
    never has epoch 0, so such a cookie cannot sign in to it."""
    auth = make_auth(tmp_path)
    getattr(auth, create)("admin", PASSWORD)
    legacy = auth._serializer.dumps({"u": "admin"})
    zero = auth._serializer.dumps({"u": "admin", "e": 0})

    assert auth.read_session(legacy) is None
    assert auth.read_session(zero) is None


# ----------------------------------------------------- both create paths


@pytest.mark.parametrize("create", ["bootstrap", "create_user"])
def test_new_account_epoch_is_in_the_json_safe_random_range(
    tmp_path: Path, create: str
) -> None:
    auth = make_auth(tmp_path)
    getattr(auth, create)("admin", PASSWORD)

    epoch = stored_epoch(tmp_path, "admin")
    assert EPOCH_FLOOR <= epoch < EPOCH_CEILING
    assert float(epoch) == epoch  # survives a parse as an IEEE double
    # The cookie carries it intact, and a session issued now is accepted.
    token = auth.issue_session("admin")
    assert auth._serializer.loads(token)["e"] == epoch
    assert auth.read_session(token) == "admin"


def test_each_new_account_gets_its_own_epoch(tmp_path: Path) -> None:
    """Random, not a new fixed constant: a fixed starting value would reopen
    the same hole one release later. (Chance of a false failure: about 2**-51
    per pair.)"""
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", PASSWORD)
    for index in range(4):
        auth.create_user(f"user{index}", PASSWORD)
    epochs = [stored_epoch(tmp_path, name) for name in accounts(tmp_path)]
    assert len(set(epochs)) == len(epochs) == 5


# ----------------------------------------- revoke and change from a random start


def test_revoke_and_change_password_count_up_from_the_random_start(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", PASSWORD)
    start = stored_epoch(tmp_path, "admin")
    token = auth.issue_session("admin")

    auth.revoke_sessions("admin")
    assert stored_epoch(tmp_path, "admin") == start + 1
    assert auth.read_session(token) is None

    _, token = auth.login("admin", PASSWORD)
    assert auth.read_session(token) == "admin"
    auth.change_password("admin", PASSWORD, SECOND)
    assert stored_epoch(tmp_path, "admin") == start + 2
    assert auth.read_session(token) is None

    _, token = auth.login("admin", SECOND)
    assert auth.read_session(token) == "admin"
    assert start + 2 < EPOCH_CEILING


# ------------------------------------------- upgrade: nobody is signed out


@pytest.mark.parametrize(
    ("stored", "cookie_payload"),
    [
        pytest.param(None, {"u": "admin"}, id="no-epoch-field-legacy-cookie"),
        pytest.param(None, {"u": "admin", "e": 0}, id="no-epoch-field-epoch-0-cookie"),
        pytest.param(0, {"u": "admin"}, id="epoch-0-legacy-cookie"),
        pytest.param(0, {"u": "admin", "e": 0}, id="epoch-0"),
        pytest.param(7, {"u": "admin", "e": 7}, id="epoch-7"),
    ],
)
def test_existing_accounts_keep_their_epoch_and_their_sessions(
    tmp_path: Path, stored: int | None, cookie_payload: dict[str, object]
) -> None:
    """An account file written by an older release (epoch 0, a small count,
    or no field at all) is used as it is: no rewrite on start, and the
    cookies it issued stay valid."""
    seed = make_auth(tmp_path)
    record: dict[str, object] = {
        "password_hash": seed.hasher.hash(PASSWORD),
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    if stored is not None:
        record["session_epoch"] = stored
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"admin": record}), encoding="utf-8")
    before = users.read_bytes()

    upgraded = make_auth(tmp_path)  # same secret file: an image upgrade
    token = upgraded._serializer.dumps(cookie_payload)

    assert upgraded.read_session(token) == "admin"
    _, signed_in = upgraded.login("admin", PASSWORD)
    assert upgraded.read_session(signed_in) == "admin"
    assert users.read_bytes() == before, "an upgrade must not rewrite existing epochs"

    # Revoking still counts up from the stored value, not from a random one.
    upgraded.revoke_sessions("admin")
    assert stored_epoch(tmp_path, "admin") == (stored or 0) + 1
    assert upgraded.read_session(token) is None


def test_existing_account_untouched_when_another_is_created(tmp_path: Path) -> None:
    """Creating a second account rewrites the file; the first account's
    epoch, and so its sessions, must come through unchanged."""
    seed = make_auth(tmp_path)
    (tmp_path / "users.json").write_text(
        json.dumps({"admin": {"password_hash": seed.hasher.hash(PASSWORD), "session_epoch": 0}}),
        encoding="utf-8",
    )
    auth = make_auth(tmp_path)
    legacy = auth._serializer.dumps({"u": "admin"})

    auth.create_user("ops", PASSWORD)

    assert stored_epoch(tmp_path, "admin") == 0
    assert auth.read_session(legacy) == "admin"
    assert stored_epoch(tmp_path, "ops") >= EPOCH_FLOOR
