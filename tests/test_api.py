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
        # Never read the repo's .env: tests describe their own world.
        _env_file=None,
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        poll_slots_seconds=0.1,
        # The fixture is a synthetic sysfs tree with no real SES device, so pin
        # the sysfs writer. 'auto' would pick the SES command path wherever
        # sg_ses happens to be installed (it is, on GitHub runners) and then
        # fail against a device node that does not exist.
        ident_method="sysfs",
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
    from ktnmgr import __version__

    body = auth_client.get("/api/diagnostics").json()
    # Asserted against the package version, not a literal, so the reported
    # version cannot drift away from the release again.
    assert body["app_version"] == __version__
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


# --------------------------------------------- session invalidation (v1.1.2)


def test_changing_password_invalidates_existing_sessions(client: TestClient) -> None:
    """The action a user takes when they suspect compromise must be the action
    that ends the attacker's access."""
    new_password = "an-even-longer-password"
    client.post("/api/auth/bootstrap",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    client.post("/api/auth/login",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    assert client.get("/api/enclosures").status_code == 200

    changed = client.post("/api/auth/password", headers=CSRF,
                          json={"current_password": PASSWORD, "new_password": new_password})
    assert changed.status_code == 200

    # The cookie the client still holds must now be refused.
    assert client.get("/api/enclosures").status_code == 401

    client.post("/api/auth/login",
                json={"username": "admin", "password": new_password}, headers=CSRF)
    assert client.get("/api/enclosures").status_code == 200


def test_pre_epoch_sessions_survive_an_upgrade(tmp_path: Path) -> None:
    """Accounts and cookies created before the epoch field existed must keep
    working, or upgrading would sign everyone out."""
    from ktnmgr.services.auth import AuthService

    users = tmp_path / "users.json"
    auth = AuthService(users, tmp_path / "secret", None, 3600, 5, 60)
    auth.create_user("admin", PASSWORD)

    # Simulate a pre-upgrade account file and a pre-upgrade cookie payload.
    import json as _json
    record = _json.loads(users.read_text())
    record["admin"].pop("session_epoch", None)
    users.write_text(_json.dumps(record))
    legacy_token = auth._serializer.dumps({"u": "admin"})

    assert auth.read_session(legacy_token) == "admin"


def test_revoke_sessions_without_password_change(tmp_path: Path) -> None:
    from ktnmgr.services.auth import AuthService

    auth = AuthService(tmp_path / "users.json", tmp_path / "secret", None, 3600, 5, 60)
    auth.create_user("admin", PASSWORD)
    token = auth.issue_session("admin")
    assert auth.read_session(token) == "admin"

    auth.revoke_sessions("admin")
    assert auth.read_session(token) is None


# Endpoints that are unauthenticated on purpose. Everything else under /api
# must reject an anonymous caller, and the test below enumerates the app's own
# routes so adding a new endpoint cannot quietly skip the gate.
PUBLIC_API_ROUTES = {
    "/api/auth/status",     # tells the UI whether to show login or bootstrap
    "/api/auth/bootstrap",  # necessarily open: there is no account yet
    "/api/auth/login",
    "/api/auth/logout",     # only clears a cookie
}


def test_every_api_route_is_either_public_by_design_or_authenticated(
    client: TestClient,
) -> None:
    """Derived from the app's own route list, not a hand-kept one.

    The auth check on a read endpoint is a `user: CurrentUser` parameter that
    the body never references. It looks unused, and deleting it removes
    authentication silently - nothing in the signature says it is load-bearing.
    A hand-maintained list of paths to probe had already drifted:
    /api/raw/pages was authenticated but untested.

    The OpenAPI schema is used rather than `app.routes` because an included
    APIRouter appears there as one opaque entry, so a naive walk finds no /api
    endpoints at all and the test passes by checking nothing.
    """
    paths = client.app.openapi()["paths"]
    checked = 0

    for path, operations in paths.items():
        if not path.startswith("/api/") or path in PUBLIC_API_ROUTES:
            continue

        concrete = (
            path.replace("{enclosure_id}", LOGICAL_ID)
            .replace("{ses_slot}", "0")
            .replace("{page}", "join")
        )
        assert "{" not in concrete, f"unsubstituted path parameter in {path}"

        for method in operations:
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            response = client.request(method.upper(), concrete, json={}, headers=CSRF)
            assert response.status_code == 401, (
                f"{method.upper()} {concrete} answered {response.status_code} "
                "without authentication"
            )
            checked += 1

    assert checked >= 8, f"only {checked} routes probed; the walk found too little"
