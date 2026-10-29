"""The app and the entrypoint must agree on whether TLS is on - by name too.

pydantic-settings matches environment variable names case-insensitively, so
the app reads ``ktn_tls_enabled=true`` as TLS on. The shell entrypoint (and
the image HEALTHCHECK) read the exact name ``KTN_TLS_ENABLED``, so for them
TLS is off. Without a cross-check the result was the one outcome the
fail-closed design exists to prevent: uvicorn serving plain HTTP while the app
and ``/healthz`` reported ``tls.enabled: true``.

The cross-check is a marker: the entrypoint exports ``KTN_TLS_ACTIVE=1`` only
after it has built uvicorn's ``--ssl-*`` arguments, and the app refuses to
start when the marker and its own setting disagree. These tests start a real
``python -m uvicorn`` (via tls_certs.run_server) and check both directions,
each against a control that does start.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from pathlib import Path

import pytest
from tls_certs import make_pair, run_server


def _pair_env(tmp_path: Path, name: str) -> dict[str, str]:
    """TLS settings under the given spelling of the KTN_TLS_ENABLED name."""
    cert, key = make_pair(tmp_path / "pair")
    return {name: "true", "KTN_TLS_CERTFILE": str(cert), "KTN_TLS_KEYFILE": str(key)}


@pytest.mark.parametrize("name", ["ktn_tls_enabled", "Ktn_Tls_Enabled", "KTN_TLS_ENABLED"])
def test_tls_on_in_the_app_without_the_entrypoint_marker_refuses(
    tmp_path: Path, name: str
) -> None:
    """Plain uvicorn (no --ssl-*, no marker) while the app's setting says on.

    The lowercase and mixed-case names are what the entrypoint misses; the
    upper-case one is a hand-run uvicorn that skipped the entrypoint.
    """
    env = _pair_env(tmp_path, name)
    with (
        pytest.raises(AssertionError, match="server exited early") as excinfo,
        run_server(tmp_path, env=env),
    ):
        pass
    log = str(excinfo.value)
    assert "KTN_TLS_ACTIVE" in log
    assert "exact upper case" in log
    # Never the key material, even in a refusal.
    assert "-----BEGIN" not in log and "PRIVATE KEY" not in log


def test_the_marker_with_tls_off_in_the_app_refuses(tmp_path: Path) -> None:
    """The reverse: the entrypoint served TLS, but the app's setting is off.

    This happens with KTN_TLS_ENABLED=true plus a stray, differently-cased
    ``ktn_tls_enabled=false`` that pydantic happens to prefer: TLS is served,
    but no renewal watcher is built, so a renewal is silently never installed.
    """
    with (
        pytest.raises(AssertionError, match="server exited early") as excinfo,
        run_server(tmp_path, env={"KTN_TLS_ACTIVE": "1"}),
    ):
        pass
    assert "KTN_TLS_ACTIVE" in str(excinfo.value)


def test_control_plain_install_without_marker_starts(tmp_path: Path) -> None:
    """A default install: TLS unset, no marker - starts and serves plain HTTP."""
    with (
        run_server(tmp_path) as port,
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r,
    ):
        body = json.loads(r.read())
    assert body["ok"] is True and "tls" not in body


def test_control_tls_with_marker_starts(tmp_path: Path) -> None:
    """TLS on in both: run_server passes --ssl-* and sets the marker, as the
    entrypoint does, and the app starts and reports TLS."""
    cert, key = make_pair(tmp_path / "pair")
    env = {"KTN_TLS_ENABLED": "true", "KTN_TLS_CERTFILE": str(cert),
           "KTN_TLS_KEYFILE": str(key)}
    with run_server(tmp_path, cert=cert, key=key, env=env) as port:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/healthz", timeout=5, context=context
        ) as r:
            body = json.loads(r.read())
    assert body["tls"]["enabled"] is True
