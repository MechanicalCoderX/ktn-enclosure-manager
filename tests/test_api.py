"""HTTP surface tests: authentication, CSRF, and endpoint behaviour."""

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
PASSWORD = "a-sufficiently-long-password"
CSRF = {"x-ktn-request": "1"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    sysfs = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    settings = Settings(
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        poll_slots_seconds=0.1,
    )
    with TestClient(build_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    client.post("/api/auth/bootstrap",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    client.post("/api/auth/login",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    return client


# ------------------------------------------------------------------ auth gate


@pytest.mark.parametrize(
    "path",
    [
        "/api/enclosures",
        f"/api/enclosures/{LOGICAL_ID}/bays",
        f"/api/enclosures/{LOGICAL_ID}/chassis",
        "/api/diagnostics",
        "/api/audit",
        f"/api/raw/{LOGICAL_ID}/join",
    ],
)
def test_endpoints_require_authentication(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_identify_requires_authentication(client: TestClient) -> None:
    response = client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify", json={"on": True}, headers=CSRF
    )
    assert response.status_code == 401


def test_healthz_is_public(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


def test_bootstrap_only_once(client: TestClient) -> None:
    first = client.post("/api/auth/bootstrap",
                        json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    assert first.status_code == 200
    second = client.post("/api/auth/bootstrap",
                         json={"username": "eve", "password": PASSWORD}, headers=CSRF)
    assert second.status_code == 400


def test_short_password_rejected(client: TestClient) -> None:
    response = client.post("/api/auth/bootstrap",
                           json={"username": "admin", "password": "short"}, headers=CSRF)
    assert response.status_code == 422


def test_login_rate_limited(client: TestClient) -> None:
    client.post("/api/auth/bootstrap",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    codes = [
        client.post("/api/auth/login",
                    json={"username": "admin", "password": "wrong"}, headers=CSRF).status_code
        for _ in range(8)
    ]
    assert codes.count(401) >= 5
    # Once the window is exhausted every attempt is refused, correct or not.
    blocked = client.post("/api/auth/login",
                          json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    assert blocked.status_code == 401


# ----------------------------------------------------------------------- CSRF


def test_mutating_requests_require_the_custom_header(auth_client: TestClient) -> None:
    """A cross-site form post cannot set a custom header, so its absence is
    treated as a forgery attempt (§33)."""
    response = auth_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify", json={"on": True}
    )
    assert response.status_code == 403


def test_session_cookie_flags(client: TestClient) -> None:
    client.post("/api/auth/bootstrap",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    response = client.post("/api/auth/login",
                           json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie.replace("Strict", "strict")


# ------------------------------------------------------------------ enclosure


def test_lists_enclosure(auth_client: TestClient) -> None:
    body = auth_client.get("/api/enclosures").json()
    assert len(body) == 1
    assert body[0]["logical_id"] == LOGICAL_ID
    assert body[0]["vendor"] == "EMC"


def test_bays_expose_bay_numbering_contract(auth_client: TestClient) -> None:
    bays = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()["bays"]
    populated = [b for b in bays if b["device"]]
    assert len(populated) == 15
    mapping = {b["ses_slot"]: b["display_bay"] for b in populated}
    assert mapping[0] == 1
    assert mapping[7] == 8
    assert mapping[14] == 15


def test_bay_carries_identity_and_health(auth_client: TestClient) -> None:
    bays = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()["bays"]
    bay = next(b for b in bays if b["ses_slot"] == 0)
    assert bay["device"] == "/dev/sdb"
    assert bay["disk"]["serial"] == "K1A00001"
    assert bay["health"] == "ok"
    assert bay["locate"] is False


def test_unknown_enclosure_is_404(auth_client: TestClient) -> None:
    assert auth_client.get("/api/enclosures/0xdeadbeef/bays").status_code == 404


# -------------------------------------------------------------------- identify


def test_identify_on_then_off(auth_client: TestClient) -> None:
    on = auth_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify",
        json={"on": True, "duration_seconds": 60}, headers=CSRF,
    )
    assert on.status_code == 200
    assert on.json()["locate"] is True
    assert on.json()["expires_at"] is not None

    bays = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()["bays"]
    assert next(b for b in bays if b["ses_slot"] == 0)["locate"] is True

    off = auth_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify", json={"on": False}, headers=CSRF
    )
    assert off.status_code == 200


def test_identify_rejects_arbitrary_duration(auth_client: TestClient) -> None:
    response = auth_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify",
        json={"on": True, "duration_seconds": 99999}, headers=CSRF,
    )
    assert response.status_code == 400


def test_identify_unknown_slot_is_rejected(auth_client: TestClient) -> None:
    response = auth_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/500/identify", json={"on": True}, headers=CSRF
    )
    assert response.status_code in (400, 404)


def test_identify_hostile_slot_never_reaches_handler(auth_client: TestClient) -> None:
    """These are rejected by routing/int-coercion before any application code runs.

    The status varies by input - 422 for a non-integer slot, 404/405 when path
    traversal makes the URL match a different route entirely - so the assertion
    is on the property that matters: nothing was written and nothing was
    audited.
    """
    before = len(auth_client.get("/api/audit").json())
    for slot in ("7;rm%20-rf%20/", "..%2F..%2Fetc%2Fpasswd", "$(id)", "0,0%20--clear=fault"):
        response = auth_client.post(
            f"/api/enclosures/{LOGICAL_ID}/slots/{slot}/identify",
            json={"on": True}, headers=CSRF,
        )
        assert response.status_code >= 400, f"{slot!r} was accepted"
        assert response.status_code != 200

    assert len(auth_client.get("/api/audit").json()) == before, "a hostile input was acted on"
    bays = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()["bays"]
    assert all(not b["locate"] for b in bays), "a hostile input lit an LED"


def test_identify_is_audited(auth_client: TestClient) -> None:
    auth_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/2/identify",
        json={"on": True, "duration_seconds": 10}, headers=CSRF,
    )
    entries = auth_client.get("/api/audit").json()
    assert entries
    latest = entries[0]
    assert latest["operation"] == "IDENT_ON"
    assert latest["ses_slot"] == 2
    assert latest["bay"] == 3
    assert latest["user"] == "admin"
    assert latest["verification"] == "success"


# ----------------------------------------------------------------- diagnostics


def test_diagnostics_has_no_secrets(auth_client: TestClient) -> None:
    """Checked structurally rather than by substring: filesystem paths in the
    payload can legitimately contain the word 'secret'."""
    payload = auth_client.get("/api/diagnostics").json()

    def walk_keys(node: object) -> list[str]:
        if isinstance(node, dict):
            return [k for k in node] + [
                key for value in node.values() for key in walk_keys(value)
            ]
        if isinstance(node, list):
            return [key for item in node for key in walk_keys(item)]
        return []

    for key in walk_keys(payload):
        assert not any(
            token in key.lower() for token in ("api_key", "apikey", "password", "token")
        ), f"diagnostics exposes a credential-shaped field: {key}"
    assert "session_secret" not in walk_keys(payload)


def test_diagnostics_reports_discovery(auth_client: TestClient) -> None:
    body = auth_client.get("/api/diagnostics").json()
    assert body["app_version"] == "1.0.0"
    enclosure = body["enclosures"][0]
    assert enclosure["logical_id"] == LOGICAL_ID
    assert enclosure["slots_discovered"] == 16


def test_raw_pages_are_allowlisted(auth_client: TestClient) -> None:
    pages = auth_client.get("/api/raw/pages").json()
    assert "join" in pages
    assert all("--set" not in p for p in pages)
    assert auth_client.get(f"/api/raw/{LOGICAL_ID}/--set=ident").status_code == 404


def test_chassis_degrades_without_sg_ses(auth_client: TestClient) -> None:
    """§37: no sg_ses must not break the bay map, only the chassis section."""
    body = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/chassis").json()
    assert body["available"] is False
    assert auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").status_code == 200
