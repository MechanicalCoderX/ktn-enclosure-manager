"""Optional authentication, and the write that stays shut without it.

Open, unauthenticated dashboards are the norm for this category on TrueNAS:
scrutiny, glances, homepage and speedtest-tracker all serve disk telemetry
with no credentials, and scrutiny publishes the same class of data this app
does (serial, WWN, SMART) - verified live, its /api/summary answers 200 to an
anonymous caller.

What none of them do is write. This app actuates an LED, so the read surface
and the write are gated separately: opening the dashboard must never open the
write as a side effect.
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
LOGICAL_ID = "0x50060480aabbcc00"
CSRF = {"x-ktn-request": "1"}

READ_PATHS = [
    "/api/enclosures",
    f"/api/enclosures/{LOGICAL_ID}/bays",
    f"/api/enclosures/{LOGICAL_ID}/chassis",
    "/api/diagnostics",
    "/api/audit",
    "/api/raw/pages",
]


def make_client(tmp_path: Path, **overrides: object) -> Iterator[TestClient]:
    sysfs = tmp_path / "sys"
    if not sysfs.exists():
        shutil.copytree(FIXTURE_ROOT, sysfs)
    settings = Settings(
        _env_file=None,
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        ident_method="sysfs",
        **overrides,  # type: ignore[arg-type]
    )
    with TestClient(build_app(settings)) as client:
        yield client


@pytest.fixture
def open_client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path, auth_required=False)


@pytest.fixture
def open_client_with_ident(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path, auth_required=False, allow_anonymous_ident=True)


@pytest.fixture
def closed_client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path)


# ------------------------------------------------------------------ defaults


def test_authentication_is_required_by_default(closed_client: TestClient) -> None:
    """The safe posture must be what you get without configuring anything."""
    assert Settings(_env_file=None).auth_required is True
    assert Settings(_env_file=None).allow_anonymous_ident is False
    assert closed_client.get("/api/enclosures").status_code == 401


# --------------------------------------------------------------- open reads


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_are_open_when_auth_is_disabled(open_client: TestClient, path: str) -> None:
    assert open_client.get(path).status_code == 200


def test_status_advertises_the_mode(open_client: TestClient) -> None:
    """The UI skips the login screen based on this, so it has to be truthful."""
    body = open_client.get("/api/auth/status").json()
    assert body["auth_required"] is False
    assert body["anonymous_ident_allowed"] is False
    # No account is needed, so the UI must not be told to demand one.
    assert body["needs_bootstrap"] is False


# ------------------------------------------------------- the write stays shut


def test_ident_is_refused_to_an_anonymous_caller(open_client: TestClient) -> None:
    """Opening the dashboard must not open the LED write."""
    response = open_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify",
        json={"on": True, "duration_seconds": 30},
        headers=CSRF,
    )
    assert response.status_code == 403
    assert "KTN_ALLOW_ANONYMOUS_IDENT" in response.json()["detail"]


def test_ident_is_allowed_once_explicitly_permitted(
    open_client_with_ident: TestClient,
) -> None:
    response = open_client_with_ident.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify",
        json={"on": True, "duration_seconds": 30},
        headers=CSRF,
    )
    # Not 403: the gate is open. The fixture has no real hardware, so the write
    # itself may still fail - what matters is that it was not refused.
    assert response.status_code != 403


def test_anonymous_ident_alone_does_not_open_the_reads(tmp_path: Path) -> None:
    """allow_anonymous_ident must not imply auth_required=False."""
    for client in make_client(tmp_path, allow_anonymous_ident=True):
        assert client.get("/api/enclosures").status_code == 401


def test_csrf_header_is_still_required_when_open(open_client_with_ident: TestClient) -> None:
    """Losing the session cookie does not mean losing the cross-site guard."""
    response = open_client_with_ident.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify",
        json={"on": True, "duration_seconds": 30},
    )
    assert response.status_code == 403


def test_password_change_is_refused_with_no_account(open_client: TestClient) -> None:
    response = open_client.post(
        "/api/auth/password",
        json={"current_password": "x" * 12, "new_password": "y" * 12},
        headers=CSRF,
    )
    assert response.status_code == 403


# --------------------------------------------- bootstrap while running open


def test_no_account_can_be_created_while_the_app_runs_open(
    open_client: TestClient,
) -> None:
    """The trap this closes: while authentication is off, this endpoint is
    reachable by anyone on the network. The account they create is inert only
    until the operator turns authentication on - at which point a stranger's
    password is the administrator credential, and the operator is the one
    locked out."""
    response = open_client.post(
        "/api/auth/bootstrap",
        json={"username": "attacker", "password": "attacker-password"},
        headers=CSRF,
    )
    assert response.status_code == 403
    assert "KTN_AUTH_REQUIRED" in response.json()["detail"]

    # And nothing was created.
    assert open_client.get("/api/auth/status").json()["user"] is None


def test_bootstrap_still_works_when_auth_is_required(closed_client: TestClient) -> None:
    response = closed_client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "a-real-password"},
        headers=CSRF,
    )
    assert response.status_code == 200


# ------------------------------------------------- the sentinel is reserved


def test_the_anonymous_username_is_reserved(closed_client: TestClient) -> None:
    """`anonymous` is the sentinel for "no session", and the API compares
    against it to gate IDENT and the password change. An account holding that
    name was refused both - it failed closed, denying a legitimate user."""
    response = closed_client.post(
        "/api/auth/bootstrap",
        json={"username": "anonymous", "password": "a-real-password"},
        headers=CSRF,
    )
    assert response.status_code == 400
    assert "reserved" in response.json()["detail"]


def test_the_reservation_is_case_insensitive(closed_client: TestClient) -> None:
    for name in ("Anonymous", "ANONYMOUS", "AnOnYmOuS"):
        response = closed_client.post(
            "/api/auth/bootstrap",
            json={"username": name, "password": "a-real-password"},
            headers=CSRF,
        )
        assert response.status_code == 400, f"{name} was accepted"
