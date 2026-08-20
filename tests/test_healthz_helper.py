"""The container healthcheck must notice a dead privileged helper.

The helper runs as a background child of the entrypoint with nothing
supervising it. Before this, a helper that died left the container Healthy
while IDENT and all SES telemetry silently disappeared - the healthcheck only
ever exercised the web process. /healthz now probes the helper socket when one
is configured, so "Healthy" means the whole deployment, not half of it.
"""

from __future__ import annotations

import json
import shutil
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"


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
        auth_required=False,
        **overrides,
    )


class _FakeHelperHandler(socketserver.StreamRequestHandler):
    """Answers ses_version the way the real helper does; refuses the rest."""

    def handle(self) -> None:
        request = json.loads(self.rfile.readline(4096) or b"{}")
        if request.get("op") == "ses_version":
            body = {"ok": True, "version": "fake sg_ses 2.55"}
        else:
            body = {"ok": False, "error": "unsupported operation"}
        self.wfile.write((json.dumps(body) + "\n").encode("utf-8"))


@pytest.fixture
def fake_helper(tmp_path: Path) -> Iterator[Path]:
    socket_path = tmp_path / "ident.sock"
    server = socketserver.ThreadingUnixStreamServer(str(socket_path), _FakeHelperHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path
    finally:
        server.shutdown()
        server.server_close()


def test_healthy_with_no_helper_configured(tmp_path: Path) -> None:
    """No helper socket, nothing to probe: the old behaviour is unchanged."""
    with TestClient(build_app(make_settings(tmp_path))) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert "helper" not in response.json()


def test_healthy_when_the_helper_answers(tmp_path: Path, fake_helper: Path) -> None:
    settings = make_settings(tmp_path, ident_helper_socket=fake_helper)
    with TestClient(build_app(settings)) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["helper"] == "ok"


def test_unhealthy_when_the_helper_is_gone(tmp_path: Path) -> None:
    """A configured-but-absent socket is exactly what a crashed helper leaves."""
    settings = make_settings(
        tmp_path, ident_helper_socket=tmp_path / "never-created.sock"
    )
    with TestClient(build_app(settings)) as client:
        response = client.get("/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["helper"] == "unreachable"
