"""Negative paths for authentication: cookies that must die, accounts that
must not be revealed, and signing out.

The positive paths (bootstrap, login, a valid cookie) were pinned from the
first release, but nothing ever presented the session layer with a forged,
expired or garbage cookie - the inputs an attacker actually sends. A session
scheme is defined by what it rejects, so these tests exercise each rejection
branch of ``read_session`` directly. Alongside them, two API behaviours with
no prior coverage: a login failure must not reveal whether the account
exists, and logout must clear the cookie login set.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app
from ktnmgr.services.auth import SESSION_COOKIE, AuthError, AuthService

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
PASSWORD = "the-original-password"
CSRF = {"x-ktn-request": "1"}


def make_auth(
    tmp_path: Path, *, session_secret: str | None = None, max_age_seconds: int = 3600
) -> AuthService:
    return AuthService(
        users_path=tmp_path / "users.json",
        secret_path=tmp_path / "session-secret",
        session_secret=session_secret,
        max_age_seconds=max_age_seconds,
        rate_limit=5,
        rate_window=60,
    )


# ------------------------------------------------------------ session tokens


def test_a_token_signed_with_a_different_secret_is_rejected(tmp_path: Path) -> None:
    """The forged-cookie case: right payload, wrong key.

    Both services share the account file, so the token names a real user with
    the correct epoch - the only thing wrong with it is the signature. Anyone
    on the network can construct the payload (it is just base64 JSON); the
    signature is the entire defence, so this rejection is the one that makes
    the session cookie an authentication token rather than a suggestion.
    """
    auth = make_auth(tmp_path, session_secret="the-real-signing-key")
    auth.bootstrap("admin", PASSWORD)
    forger = make_auth(tmp_path, session_secret="not-the-real-signing-key")

    assert auth.read_session(forger.issue_session("admin")) is None


def test_an_expired_token_is_rejected(tmp_path: Path) -> None:
    """Session expiry is enforced by the reader, not the browser.

    max_age_seconds is -1, not 0: itsdangerous compares ``age > max_age``
    strictly, so with 0 a token issued within the same second still loads
    (measured, not theorised). -1 makes a just-issued token already past its
    lifetime, exercising the SignatureExpired branch deterministically
    without sleeping or faking the clock.
    """
    auth = make_auth(tmp_path, max_age_seconds=-1)
    auth.bootstrap("admin", PASSWORD)

    assert auth.read_session(auth.issue_session("admin")) is None


def test_garbage_and_absent_tokens_are_rejected(tmp_path: Path) -> None:
    """Unsigned junk: what a scanner or a hand-typed Cookie header sends."""
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", PASSWORD)

    assert auth.read_session("garbage") is None
    assert auth.read_session("") is None
    assert auth.read_session(None) is None


def test_verify_raises_for_a_nonexistent_user(tmp_path: Path) -> None:
    """The equal-cost branch for a missing account.

    ``verify`` hashes a dummy password when the user does not exist so that a
    missing user and a wrong password cost the same and raise the same error;
    this pins the "raise" half of that contract (the API-level test below
    pins the "same error" half).
    """
    auth = make_auth(tmp_path)
    auth.bootstrap("admin", PASSWORD)

    with pytest.raises(AuthError):
        auth.verify("nobody", PASSWORD)


# ------------------------------------------------------------------------ api


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
        test_client.post("/api/auth/bootstrap",
                         json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        yield test_client


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login",
                       json={"username": username, "password": password}, headers=CSRF)


def test_unknown_user_and_wrong_password_are_indistinguishable(client: TestClient) -> None:
    """No user-existence oracle at the login endpoint.

    An attacker who can tell "no such account" from "wrong password" has
    halved their problem: enumerate names first, then attack only real ones.
    Same status, same detail string, whichever half of the credential was
    wrong. (Two failed attempts - well under the rate limit of 5, so neither
    response here can be the limiter's 429.)
    """
    unknown = login(client, "nobody", PASSWORD)
    wrong = login(client, "admin", "definitely-not-the-password")

    assert unknown.status_code == 401
    assert wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_logout_clears_the_session_cookie(client: TestClient) -> None:
    """Logout must clear the cookie login set - same name, same path.

    A delete_cookie with a different path would leave the original cookie
    standing and the browser still signed in, so the path is asserted, not
    assumed.
    """
    assert login(client, "admin", PASSWORD).status_code == 200
    assert SESSION_COOKIE in client.cookies

    response = client.post("/api/auth/logout", headers=CSRF)

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert f'{SESSION_COOKIE}=""' in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=0" in set_cookie
    # The test client honours the header the way a browser would: the jar no
    # longer holds a session for subsequent requests.
    assert SESSION_COOKIE not in client.cookies
