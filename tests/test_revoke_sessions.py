"""Signing out everywhere, end to end.

The session-epoch mechanism existed since the change-password work, but no
route exposed it - the same mistake the change-password endpoint itself once
made. These tests pin the property the feature exists for: after a revoke, a
cookie the attacker copied earlier stops working, even though the password
never changed.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app
from ktnmgr.services.auth import SESSION_COOKIE

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
PASSWORD = "the-original-password"
CSRF = {"x-ktn-request": "1"}


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    sysfs = tmp_path / "sys"
    if not sysfs.exists():
        shutil.copytree(FIXTURE_ROOT, sysfs)
    return Settings(
        _env_file=None,
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        ident_method="sysfs",
        **overrides,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(make_settings(tmp_path))) as test_client:
        test_client.post("/api/auth/bootstrap",
                         json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        test_client.post("/api/auth/login",
                         json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        yield test_client


def test_a_previously_stolen_cookie_stops_working(client: TestClient) -> None:
    stolen = client.cookies[SESSION_COOKIE]
    assert client.get("/api/enclosures").status_code == 200

    assert client.post("/api/auth/revoke-sessions", headers=CSRF).status_code == 200

    # The attacker still holds the byte-identical cookie. It must be dead:
    # the signature is still valid, but the epoch inside no longer matches.
    client.cookies.set(SESSION_COOKIE, stolen)
    assert client.get("/api/enclosures").status_code == 401


def test_the_password_still_works_afterwards(client: TestClient) -> None:
    client.post("/api/auth/revoke-sessions", headers=CSRF)
    client.cookies.clear()
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": PASSWORD},
                       headers=CSRF).status_code == 200


def test_revoke_requires_a_csrf_header(client: TestClient) -> None:
    assert client.post("/api/auth/revoke-sessions").status_code == 403


def test_anonymous_caller_is_refused(tmp_path: Path) -> None:
    """With authentication off there is no account whose sessions could end."""
    settings = make_settings(tmp_path, auth_required=False)
    with TestClient(build_app(settings)) as client:
        response = client.post("/api/auth/revoke-sessions", headers=CSRF)
        assert response.status_code == 403
