"""The change-password endpoint is rate limited like login.

It verifies a password exactly like login does, but the login limiter never
saw it because no login happens there. That asymmetry mattered: this endpoint
is the one a stolen session cookie gets pointed at - the cookie expires, the
password does not, and ``current_password`` was brute-forceable without limit.
These tests pin that the same limiter now guards both, that the refusal is 429
(not "wrong password"), and that a successful change clears the counter.
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
PASSWORD = "the-original-password"
CSRF = {"x-ktn-request": "1"}
LIMIT = 3  # small, so the test does not hammer Argon2 five times


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
        login_rate_limit=LIMIT,
        login_rate_window_seconds=3600,  # far longer than the test, so no decay
    )
    with TestClient(build_app(settings)) as test_client:
        test_client.post("/api/auth/bootstrap",
                         json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        test_client.post("/api/auth/login",
                         json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        yield test_client


def change(client: TestClient, current: str, new: str):
    return client.post(
        "/api/auth/password",
        json={"current_password": current, "new_password": new},
        headers=CSRF,
    )


def test_wrong_current_password_is_eventually_refused_with_429(client: TestClient) -> None:
    """Attempts up to the limit say 400 (wrong password); past it, 429."""
    for _ in range(LIMIT):
        assert change(client, "not-the-password", "a-new-long-password").status_code == 400
    response = change(client, "not-the-password", "a-new-long-password")
    assert response.status_code == 429


def test_the_limit_blocks_even_the_correct_password(client: TestClient) -> None:
    """Once the window is full the endpoint refuses outright.

    If the correct password still went through, an attacker's guesses would
    only ever cost them nothing: keep guessing, and the one that is right
    works regardless.
    """
    for _ in range(LIMIT):
        change(client, "not-the-password", "a-new-long-password")
    response = change(client, PASSWORD, "a-new-long-password")
    assert response.status_code == 429


def test_a_successful_change_resets_the_counter(client: TestClient) -> None:
    for _ in range(LIMIT - 1):
        change(client, "not-the-password", "a-new-long-password")
    assert change(client, PASSWORD, "a-fresh-long-password").status_code == 200
    # The change signed every session out (epoch bump); sign back in and
    # confirm the window is genuinely empty rather than one-off lucky.
    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "admin", "password": "a-fresh-long-password"},
                headers=CSRF)
    for _ in range(LIMIT - 1):
        assert change(client, "still-wrong", "another-long-password").status_code == 400
