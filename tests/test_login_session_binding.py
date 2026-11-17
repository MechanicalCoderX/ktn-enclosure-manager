"""A sign-in's session is bound to the account state it actually verified.

Sign-in used to be two steps: verify the password (one Argon2 verify, a few
hundred milliseconds at the default parameters), then issue a session by
re-reading the account file for the session epoch. A password change that
committed between the two gave the old-password sign-in a cookie carrying the
NEW epoch - a session that survived the change meant to end it. A successful
sign-in also resets the rate limiter, so someone who already holds the old
password could sign in back to back and keep that window open almost all the
time: the independent review measured 85 of 85 attempts keeping a session.

The tests force the change to land inside the sign-in's verify, which is
exactly the real window. No test sleeps to "let a race happen".
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app
from ktnmgr.services.auth import AuthError, AuthService

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
OLD = "the-original-password"
NEW = "a-different-password"
CSRF = {"x-ktn-request": "1"}
JOIN_TIMEOUT = 20.0


def fast_hasher(time_cost: int = 1) -> PasswordHasher:
    """Argon2 at minimal cost: these tests are about ordering, not hashing."""
    return PasswordHasher(time_cost=time_cost, memory_cost=8, parallelism=1)


class ChangeDuringVerify:
    """Wraps a PasswordHasher. The first ``verify`` it sees runs to completion
    and then, before returning, a password change from OLD to NEW is run to
    completion on another thread - i.e. the change commits while the sign-in
    is still between verifying and issuing its session.

    The change's own verify comes through here too; only the first call
    fires, so it passes straight through.
    """

    def __init__(self, inner: PasswordHasher, auth: AuthService) -> None:
        self._inner = inner
        self._auth = auth
        self.fired = False
        self.change_errors: list[BaseException] = []

    def verify(self, hash_: str, password: str) -> bool:
        ok = self._inner.verify(hash_, password)
        if not self.fired:
            self.fired = True
            changer = threading.Thread(target=self._change, daemon=True)
            changer.start()
            changer.join(JOIN_TIMEOUT)
            assert not changer.is_alive(), "the password change hung"
        return ok

    def _change(self) -> None:
        try:
            self._auth.change_password("admin", OLD, NEW)
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            self.change_errors.append(exc)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


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


def password_works(auth: AuthService, password: str) -> bool:
    try:
        auth.verify("admin", password)
    except AuthError:
        return False
    return True


# ------------------------------------------------------------ service level


def test_a_password_change_during_sign_in_leaves_that_session_dead(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.create_user("admin", OLD)
    inner = auth.hasher
    hook = ChangeDuringVerify(inner, auth)
    auth.hasher = hook  # type: ignore[assignment]

    username, token = auth.login("admin", OLD)

    auth.hasher = inner
    assert hook.fired, "the hook never fired: the race was not exercised"
    assert not hook.change_errors, f"password change failed: {hook.change_errors!r}"
    assert password_works(auth, NEW) and not password_works(auth, OLD)
    assert username == "admin"
    assert auth.read_session(token) is None, (
        "a sign-in verified with the old password got a session that survives "
        "the password change"
    )
    # Control: with no change in flight, a sign-in gets a working session.
    _, fresh = auth.login("admin", NEW)
    assert auth.read_session(fresh) == "admin"


def test_sign_in_with_a_wrong_password_is_refused(tmp_path: Path) -> None:
    auth = make_auth(tmp_path)
    auth.create_user("admin", OLD)
    with pytest.raises(AuthError, match="invalid credentials"):
        auth.login("admin", NEW)
    with pytest.raises(AuthError, match="invalid credentials"):
        auth.login("nobody", OLD)


def test_sign_in_that_rehashes_still_gets_a_live_session(tmp_path: Path) -> None:
    """Binding the session to the verified record must not break the
    opportunistic rehash: a rehash changes the hash, never the epoch."""
    auth = make_auth(tmp_path)
    auth.hasher = fast_hasher(time_cost=1)
    auth.create_user("admin", OLD)
    auth.hasher = fast_hasher(time_cost=2)

    _, token = auth.login("admin", OLD)

    assert auth.read_session(token) == "admin"
    stored = auth._read_users()["admin"]["password_hash"]
    assert not auth.hasher.check_needs_rehash(stored), "the rehash did not happen"


# --------------------------------------------------------------- HTTP level


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    sysfs = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    settings = Settings(
        _env_file=None,
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        ident_method="sysfs",
    )
    with TestClient(build_app(settings)) as test_client:
        test_client.app.state.auth.hasher = fast_hasher()  # type: ignore[attr-defined]
        assert test_client.post(
            "/api/auth/bootstrap", json={"username": "admin", "password": OLD}, headers=CSRF
        ).status_code == 200
        yield test_client


def test_the_login_route_issues_the_session_bound_to_what_it_verified(
    client: TestClient,
) -> None:
    """The same race through the real route, so the fix cannot sit unused in
    the service while /auth/login still verifies and then re-reads."""
    auth = client.app.state.auth  # type: ignore[attr-defined]
    inner = auth.hasher
    hook = ChangeDuringVerify(inner, auth)
    auth.hasher = hook

    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": OLD}, headers=CSRF
    )

    auth.hasher = inner
    assert hook.fired and not hook.change_errors
    assert response.status_code == 200
    assert client.cookies.get("ktn_session"), "control: the login set a cookie"
    assert client.get("/api/auth/status").json()["user"] is None
    assert client.get("/api/enclosures").status_code == 401

    # Control: an unraced login through the same route gets a working session.
    client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": NEW}, headers=CSRF
    ).status_code == 200
    assert client.get("/api/auth/status").json()["user"] == "admin"
    assert client.get("/api/enclosures").status_code == 200
