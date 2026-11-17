"""/api/diagnostics shows KTN_TRUENAS_URL as scheme, host and port only.

It used to return the URL verbatim. A path, query or userinfo in it can carry
a credential (a token in the query string, user:password@host), and
diagnostics is meant to be copied into bug reports; with
KTN_AUTH_REQUIRED=false anyone who can reach the dashboard can read it. The
same withholding rule as the startup log line applies: when the URL contains
"@" the host is withheld too. Every value here is synthetic.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings, truenas_url_for_display
from ktnmgr.main import build_app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
PASSWORD = "a-sufficiently-long-password"
CSRF = {"x-ktn-request": "1"}
SECRET = "synthetic-d1a9-secret"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://nas.example.invalid", "https://nas.example.invalid"),
        ("https://nas.example.invalid:8443/", "https://nas.example.invalid:8443"),
        (f"https://nas.example.invalid/api/{SECRET}", "https://nas.example.invalid"),
        (f"https://nas.example.invalid/?token={SECRET}", "https://nas.example.invalid"),
        (f"https://nas.example.invalid#{SECRET}", "https://nas.example.invalid"),
        ("WSS://NAS.example.invalid", "wss://nas.example.invalid"),
        ("  https://nas.example.invalid  ", "https://nas.example.invalid"),
        (
            f"https://admin:{SECRET}@nas.example.invalid",
            "https://<host withheld: URL contains userinfo>",
        ),
        # An unencoded "/" in the password ends urlsplit's netloc early, so
        # its "hostname" would be the username: withheld, not shown.
        (
            f"https://admin:{SECRET}/x@nas.example.invalid",
            "https://<host withheld: URL contains userinfo>",
        ),
        ("https://[::1", "https://<unparsable host>"),
        ("", None),
        ("   ", None),
    ],
)
def test_display_form(url: str, shown: str | None) -> None:
    assert truenas_url_for_display(url) == shown
    if shown is not None:
        assert SECRET not in shown


def test_a_bad_port_is_not_shown_and_does_not_raise() -> None:
    assert truenas_url_for_display("https://nas.example.invalid:99999") == (
        "https://nas.example.invalid"
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    sysfs = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path / "data",
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        ident_method="sysfs",
        # No API key: no client is built and nothing connects, but the URL is
        # still reported, which is the case diagnostics has to get right.
        truenas_url=f"https://admin:{SECRET}@nas.example.invalid/x?token={SECRET}",
        poll_slots_seconds=3600.0,
    )
    with TestClient(build_app(s)) as c:
        c.post("/api/auth/bootstrap",
               json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        c.post("/api/auth/login",
               json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        yield c


def test_diagnostics_never_returns_the_credential(client: TestClient) -> None:
    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert response.json()["truenas_url"] == (
        "https://<host withheld: URL contains userinfo>"
    )
