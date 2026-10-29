"""docker-tlscheck.py: the pair check the entrypoint runs before serving.

A valid pair passes, a mismatched key fails without printing any PEM text,
and an expired certificate passes with a WARNING (it is still served - see
the script's docstring).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tls_certs import ROOT, make_expired_pair, make_pair

SCRIPT = ROOT / "docker-tlscheck.py"


def run_check(cert: Path, key: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(cert), str(key)],
        capture_output=True, text=True, timeout=30, check=False,
    )


def test_a_valid_pair_passes(tmp_path: Path) -> None:
    cert, key = make_pair(tmp_path)
    result = run_check(cert, key)
    assert result.returncode == 0, result.stderr
    assert "CN=truenas.example.test" in result.stdout
    assert "days left" in result.stdout


def test_a_mismatched_key_fails_and_prints_no_pem(tmp_path: Path) -> None:
    cert, _ = make_pair(tmp_path, "a")
    _, other_key = make_pair(tmp_path, "b")
    result = run_check(cert, other_key)
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "TLS check failed" in output
    assert "-----BEGIN" not in output and "PRIVATE KEY" not in output
    # The key's own base64 body must not appear either, even partially.
    body = "".join(other_key.read_text().splitlines()[1:-1])
    assert body[:24] not in output


def test_a_non_pem_file_fails(tmp_path: Path) -> None:
    cert, key = make_pair(tmp_path)
    garbage = tmp_path / "garbage.crt"
    garbage.write_text("not a certificate\n")
    assert run_check(garbage, key).returncode == 1
    # Control: the same key with its real certificate passes.
    assert run_check(cert, key).returncode == 0


def test_a_missing_file_fails(tmp_path: Path) -> None:
    cert, _ = make_pair(tmp_path)
    assert run_check(cert, tmp_path / "absent.key").returncode == 1


def test_an_expired_certificate_warns_but_passes(tmp_path: Path) -> None:
    cert, key = make_expired_pair(tmp_path)
    result = run_check(cert, key)
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stdout and "EXPIRED" in result.stdout
    # Control: an in-date certificate carries no warning.
    good_cert, good_key = make_pair(tmp_path)
    assert "WARNING" not in run_check(good_cert, good_key).stdout
