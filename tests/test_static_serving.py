"""Regression tests for the SPA fallback route.

This route is unauthenticated, so a containment bug in it is an arbitrary file
read by anyone who can reach the port. It was exactly that until 1.1.1: the
handler did `FRONTEND_DIR / path` with no check, and pathlib does not confine
the result - an absolute path replaces the base and `..` walks out of it.
Confirmed against a live instance to serve /etc/passwd, the session signing
key, and the account file with its password hashes.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import ktnmgr.main as main_module
import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"

#: Every encoding of "escape the bundle" worth testing. `--path-as-is` style
#: requests reach the app without the client normalising them away.
TRAVERSALS = [
    "/etc/passwd",
    "//etc/passwd",
    "../../../../etc/passwd",
    "..%2f..%2f..%2f..%2fetc%2fpasswd",
    "%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
    "....//....//....//etc/passwd",
    "/data/session-secret",
    "//data/users.json",
    "../secret.txt",
    "..\\..\\secret.txt",
]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app whose frontend bundle sits next to a file it must never serve."""
    bundle = tmp_path / "dist"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html><title>spa</title>")
    (bundle / "assets" / "app.js").write_text("console.log('ok')")

    # A sibling of the bundle, i.e. one `..` away.
    (tmp_path / "secret.txt").write_text("TOP-SECRET-SENTINEL")

    monkeypatch.setattr(main_module, "FRONTEND_DIR", bundle)

    sysfs = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    settings = Settings(
        # Never read the repo's .env: tests describe their own world.
        _env_file=None,
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        ident_method="sysfs",
    )
    with TestClient(build_app(settings)) as test_client:
        yield test_client


@pytest.mark.parametrize("path", TRAVERSALS)
def test_traversal_never_serves_a_file_outside_the_bundle(
    client: TestClient, path: str
) -> None:
    response = client.get(f"/{path}")
    # The SPA fallback legitimately answers 200 for unknown routes, so status
    # alone proves nothing - assert on the body.
    assert "root:x:0:0" not in response.text, f"{path} leaked /etc/passwd"
    assert "TOP-SECRET-SENTINEL" not in response.text, f"{path} leaked a sibling file"
    assert "password_hash" not in response.text, f"{path} leaked the account file"
    assert "<title>spa</title>" in response.text, f"{path} did not fall back to index.html"


def test_absolute_path_does_not_replace_the_bundle_root(client: TestClient) -> None:
    """pathlib's `/` operator discards the base when the right side is absolute.
    That single behaviour was the whole vulnerability."""
    assert (Path("/a/b") / "/etc/passwd") == Path("/etc/passwd")
    assert "root:x:0:0" not in client.get("//etc/passwd").text


def test_symlink_inside_the_bundle_cannot_point_out(
    client: TestClient, tmp_path: Path
) -> None:
    """Resolution happens before the containment check, so a planted symlink is
    caught too."""
    link = tmp_path / "dist" / "escape.txt"
    link.symlink_to(tmp_path / "secret.txt")
    assert "TOP-SECRET-SENTINEL" not in client.get("/escape.txt").text


def test_legitimate_bundle_files_are_still_served(client: TestClient) -> None:
    assert "console.log" in client.get("/assets/app.js").text
    assert "<title>spa</title>" in client.get("/").text


def test_unknown_route_falls_back_to_the_spa(client: TestClient) -> None:
    """Client-side routes must still load the app rather than 404."""
    response = client.get("/some/deep/client/route")
    assert response.status_code == 200
    assert "<title>spa</title>" in response.text


def test_api_routes_are_not_shadowed_by_the_fallback(client: TestClient) -> None:
    assert client.get("/api/enclosures").status_code == 401
    assert client.get("/healthz").status_code == 200
