"""Throwaway certificates and a real uvicorn server for the TLS tests.

Everything is generated per test into tmp_path with the openssl CLI: no
certificate or key is committed, and none of this material is ever printed.
openssl is required rather than skipped - a missing tool must fail loudly,
not turn the TLS tests into silent passes.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
HOSTNAME = "truenas.example.test"


def make_pair(directory: Path, name: str = "server", *, days: int = 30,
              hostname: str = HOSTNAME) -> tuple[Path, Path]:
    """An EC P-256 self-signed cert for ``hostname`` and its key."""
    assert shutil.which("openssl"), "the TLS tests need the openssl CLI"
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-keyout", str(key), "-out", str(cert), "-days", str(days),
         "-subj", f"/CN={hostname}", "-addext", f"subjectAltName=DNS:{hostname}"],
        check=True, capture_output=True,
    )
    return cert, key


def make_expired_pair(directory: Path, name: str = "expired") -> tuple[Path, Path]:
    """A pair whose certificate's notAfter is already in the past.

    ``openssl req`` cannot back-date before OpenSSL 3.4 (CI's runner has 3.0)
    and rejects a negative ``-days``, so this self-signs through ``openssl
    ca``, whose -startdate/-enddate work on every 3.x.
    """
    assert shutil.which("openssl"), "the TLS tests need the openssl CLI"
    ca = directory / f"{name}-ca"
    ca.mkdir(parents=True)
    (ca / "index.txt").write_text("")
    (ca / "serial").write_text("01\n")
    config = ca / "ca.cnf"
    config.write_text(
        "[ca]\ndefault_ca = d\n[d]\n"
        f"database = {ca}/index.txt\nnew_certs_dir = {ca}\nserial = {ca}/serial\n"
        "default_md = sha256\npolicy = p\n[p]\ncommonName = supplied\n"
    )
    cert, key, csr = directory / f"{name}.crt", directory / f"{name}.key", ca / "req.csr"
    subprocess.run(
        ["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={HOSTNAME}"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "ca", "-batch", "-config", str(config), "-selfsign", "-keyfile", str(key),
         "-in", str(csr), "-out", str(cert), "-notext",
         "-startdate", "20200101000000Z", "-enddate", "20200201000000Z"],
        check=True, capture_output=True,
    )
    return cert, key


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def run_server(tmp_path: Path, *, cert: Path | None = None, key: Path | None = None,
               env: dict[str, str] | None = None, name: str = "srv") -> Iterator[int]:
    """Run ``python -m uvicorn ktnmgr.main:app`` for real; yield its port.

    TLS when a cert and key are given, plain HTTP otherwise. The app reads
    its settings from the environment, as in the container; cwd is a fresh
    directory so no developer .env can leak in.
    """
    work = tmp_path / name
    work.mkdir()
    sysfs = work / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    port = free_port()
    # Case-insensitive, as pydantic-settings reads them.
    child_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("KTN_")}
    if cert is not None and key is not None:
        # What docker-entrypoint.sh exports once it has built the --ssl-*
        # arguments; the app refuses to start on TLS without it.
        child_env["KTN_TLS_ACTIVE"] = "1"
    child_env.update(
        PYTHONPATH=str(ROOT / "backend"),
        KTN_SYSFS_ROOT=str(sysfs),
        KTN_DEV_ROOT=str(work / "dev"),
        KTN_DATA_DIR=str(work / "data"),
        KTN_TRUENAS_URL="",
        KTN_IDENT_METHOD="sysfs",
        KTN_POLL_SES_SECONDS="3600",
    )
    child_env.update(env or {})
    argv = [sys.executable, "-m", "uvicorn", "ktnmgr.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    if cert is not None and key is not None:
        argv += ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)]
    log_path = work / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=work, env=child_env, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise AssertionError(f"server exited early:\n{log_path.read_text()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError(
                        f"server never listened:\n{log_path.read_text()}"
                    ) from None
                time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
