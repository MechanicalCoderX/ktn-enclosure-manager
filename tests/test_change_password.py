"""Changing a password, end to end.

The endpoint and its API client existed from the first release but nothing
rendered them, so the feature was unreachable from the application. These
tests pin the behaviour the UI now depends on - in particular that the change
really does invalidate the session that made it, which is what the dialog
tells the operator will happen.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
OLD = "the-original-password"
NEW = "a-brand-new-password"
CSRF = {"x-ktn-request": "1"}


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
                         json={"username": "admin", "password": OLD}, headers=CSRF)
        test_client.post("/api/auth/login",
                         json={"username": "admin", "password": OLD}, headers=CSRF)
        yield test_client


def change(client: TestClient, current: str, new: str):
    return client.post(
        "/api/auth/password",
        json={"current_password": current, "new_password": new},
        headers=CSRF,
    )


def test_password_change_succeeds_and_the_new_one_works(client: TestClient) -> None:
    assert change(client, OLD, NEW).status_code == 200

    client.cookies.clear()
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": NEW},
                       headers=CSRF).status_code == 200


def test_the_old_password_stops_working(client: TestClient) -> None:
    change(client, OLD, NEW)
    client.cookies.clear()
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": OLD},
                       headers=CSRF).status_code == 401


def test_the_session_that_changed_it_is_invalidated(client: TestClient) -> None:
    """The dialog promises this, and it is the point of the epoch bump: a
    stolen cookie must stop working the moment the victim reacts."""
    assert client.get("/api/enclosures").status_code == 200
    change(client, OLD, NEW)
    assert client.get("/api/enclosures").status_code == 401


def test_a_wrong_current_password_is_refused(client: TestClient) -> None:
    assert change(client, "not-the-password", NEW).status_code == 400
    # ...and the original still works.
    client.cookies.clear()
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": OLD},
                       headers=CSRF).status_code == 200


def test_a_short_new_password_is_refused(client: TestClient) -> None:
    assert change(client, OLD, "short").status_code == 422


def test_reusing_the_current_password_is_refused(client: TestClient) -> None:
    """A change made because the old password leaked has to change it."""
    response = change(client, OLD, OLD)
    assert response.status_code == 400
    assert "differ" in response.json()["detail"]


def test_change_requires_authentication(client: TestClient) -> None:
    client.cookies.clear()
    assert change(client, OLD, NEW).status_code == 401


def test_change_requires_the_csrf_header(client: TestClient) -> None:
    response = client.post(
        "/api/auth/password",
        json={"current_password": OLD, "new_password": NEW},
    )
    assert response.status_code == 403
