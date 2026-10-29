"""docker-healthcheck.py follows KTN_TLS_ENABLED and KTN_PORT.

It must pass against a TLS instance when told TLS is on, and FAIL against
that same instance when told it is off - otherwise a passing result would
not show it had probed anything. Against a plain instance with TLS off it
passes, which is the unchanged default deployment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from tls_certs import ROOT, free_port, make_pair, run_server

SCRIPT = ROOT / "docker-healthcheck.py"


def probe(port: int | None, tls: str | None) -> int:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KTN_")}
    if port is not None:
        env["KTN_PORT"] = str(port)
    if tls is not None:
        env["KTN_TLS_ENABLED"] = tls
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, timeout=30, check=False
    ).returncode


@pytest.fixture(scope="module")
def tls_port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    tmp = tmp_path_factory.mktemp("hc-tls")
    cert, key = make_pair(tmp / "pair")
    with run_server(tmp, cert=cert, key=key) as port:
        yield port


@pytest.fixture(scope="module")
def plain_port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    with run_server(tmp_path_factory.mktemp("hc-plain")) as port:
        yield port


def test_passes_against_tls_when_tls_is_on(tls_port: int) -> None:
    assert probe(tls_port, "true") == 0


def test_fails_against_tls_when_told_tls_is_off(tls_port: int) -> None:
    """Proves the probe is not trivially passing."""
    assert probe(tls_port, "false") == 1


def test_passes_against_plain_when_tls_is_off(plain_port: int) -> None:
    assert probe(plain_port, "false") == 0
    assert probe(plain_port, None) == 0  # unset is off


def test_fails_against_plain_when_told_tls_is_on(plain_port: int) -> None:
    assert probe(plain_port, "true") == 1


def test_honours_ktn_port(plain_port: int) -> None:
    unused = free_port()
    assert probe(unused, "false") == 1
    # Control: the real port, same everything else.
    assert probe(plain_port, "false") == 0
