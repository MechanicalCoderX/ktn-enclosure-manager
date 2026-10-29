"""docker-entrypoint.sh's TLS block, run for real under sh.

The container-only pieces are replaced by small PATH shims: ``su`` (the
data-dir check, which needs the image's ktn user), ``setpriv`` (passes the
command through as the current user), ``sleep`` (instant, and logged so the
retry count is visible) and ``python -m uvicorn`` (records its arguments
instead of serving). Everything else - the boolean table, the tmpfs check,
the copy, docker-tlscheck.py and the refusal paths - is the real code.

What this cannot show is the root-to-uid-1000 hand-off, a real /run/ktn tmpfs
mount or capability-less root reading a 0400 key; the CI container smoke test
(``tls-smoke`` in .github/workflows/verify.yml) exercises those.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from tls_certs import ROOT, make_pair

ENTRYPOINT = ROOT / "docker-entrypoint.sh"

SHIMS = {
    "su": "#!/bin/sh\nexit 0\n",
    "chown": "#!/bin/sh\nexit 0\n",
    "setpriv": (
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done\n'
        'exec "$@"\n'
    ),
    "sleep": (
        "#!/bin/sh\n"
        'echo slept >> "$SHIM_DIR/sleeps"\n'
        'if [ -n "${SHIM_ON_FIRST_SLEEP:-}" ] && [ ! -e "$SHIM_DIR/ran" ]; then\n'
        '    : > "$SHIM_DIR/ran"; sh -c "$SHIM_ON_FIRST_SLEEP"\n'
        "fi\n"
    ),
    "python": (
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "uvicorn" ]; then\n'
        '    printf "%s\\n" "$@" > "$SHIM_DIR/uvicorn-args"\n'
        # The TLS marker as uvicorn would inherit it ("<unset>" if absent).
        '    printf "%s\\n" "${KTN_TLS_ACTIVE-<unset>}" > "$SHIM_DIR/tls-active"; exit 0\n'
        "fi\n"
        'exec "$REAL_PYTHON" "$@"\n'
    ),
}


@dataclass
class Run:
    returncode: int
    output: str
    uvicorn_args: list[str] | None
    sleeps: int
    #: KTN_TLS_ACTIVE as the uvicorn process saw it ("<unset>" if absent);
    #: None when uvicorn never started.
    tls_active: str | None = None


class Harness:
    def __init__(self, tmp_path: Path, runtime_parent: Path) -> None:
        self.tmp = tmp_path
        self.shims = tmp_path / "shims"
        self.shims.mkdir()
        for name, body in SHIMS.items():
            path = self.shims / name
            path.write_text(body)
            path.chmod(0o755)
        self.runtime = runtime_parent / "tls"
        self.cert, self.key = make_pair(tmp_path / "mounted")

    def run(self, **env: str) -> Run:
        # Case-insensitive: pydantic-settings would read a lowercase ktn_* too.
        child = {k: v for k, v in os.environ.items() if not k.upper().startswith("KTN_")}
        child.update(
            PATH=f"{self.shims}:{os.environ['PATH']}",
            SHIM_DIR=str(self.shims),
            REAL_PYTHON=sys.executable,
            KTN_DATA_DIR=str(self.tmp / "data"),
            KTN_ENCLOSURE_LOCK=str(self.tmp / "run-ktn" / "enclosure.lock"),
            KTN_TLS_RUNTIME_DIR=str(self.runtime),
        )
        child.update(env)
        for leftover in ("uvicorn-args", "sleeps", "ran", "tls-active"):
            (self.shims / leftover).unlink(missing_ok=True)
        result = subprocess.run(
            ["sh", str(ENTRYPOINT)], env=child, capture_output=True, text=True, timeout=60,
            check=False,
        )
        args_file = self.shims / "uvicorn-args"
        sleeps_file = self.shims / "sleeps"
        active_file = self.shims / "tls-active"
        return Run(
            returncode=result.returncode,
            output=result.stdout + result.stderr,
            uvicorn_args=args_file.read_text().splitlines() if args_file.exists() else None,
            sleeps=len(sleeps_file.read_text().splitlines()) if sleeps_file.exists() else 0,
            tls_active=active_file.read_text().strip() if active_file.exists() else None,
        )

    def enabled(self, **env: str) -> dict[str, str]:
        return {
            "KTN_TLS_ENABLED": "true",
            "KTN_TLS_CERTFILE": str(self.cert),
            "KTN_TLS_KEYFILE": str(self.key),
            **env,
        }


def fs_type(path: Path) -> str:
    return subprocess.run(
        ["stat", "-f", "-c", "%T", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    """Runtime dir on /dev/shm: a real tmpfs, as /run/ktn is in the container."""
    shm = Path("/dev/shm")
    assert fs_type(shm) == "tmpfs", "these tests need /dev/shm to be a tmpfs"
    parent = shm / f"ktn-test-{uuid.uuid4().hex}"
    parent.mkdir(mode=0o700)
    try:
        yield Harness(tmp_path, parent)
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_unset_starts_plain_http_exactly_as_before(harness: Harness) -> None:
    run = harness.run()
    assert run.returncode == 0, run.output
    assert run.uvicorn_args is not None
    assert not any(arg.startswith("--ssl") for arg in run.uvicorn_args)
    assert not harness.runtime.exists()


def test_false_is_plain_too(harness: Harness) -> None:
    run = harness.run(KTN_TLS_ENABLED="false", KTN_TLS_CERTFILE="/nonexistent")
    assert run.returncode == 0, run.output
    assert run.uvicorn_args is not None
    assert "--ssl-certfile" not in run.uvicorn_args


def test_a_valid_pair_is_copied_and_served(harness: Harness) -> None:
    run = harness.run(**harness.enabled())
    assert run.returncode == 0, run.output
    served_cert, served_key = harness.runtime / "cert.pem", harness.runtime / "key.pem"
    assert run.uvicorn_args is not None
    args = run.uvicorn_args
    assert args[args.index("--ssl-certfile") + 1] == str(served_cert)
    assert args[args.index("--ssl-keyfile") + 1] == str(served_key)
    # Copies are byte-identical (compared by hash: never print key bytes).
    assert sha(served_cert) == sha(harness.cert)
    assert sha(served_key) == sha(harness.key)
    assert stat.S_IMODE(served_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(served_cert.stat().st_mode) == 0o600
    assert stat.S_IMODE(harness.runtime.stat().st_mode) == 0o700
    assert "TLS certificate OK" in run.output
    assert not list(harness.runtime.glob("*.tmp"))
    assert run.sleeps == 0


def test_a_mismatched_pair_refuses_to_start_and_never_serves_plain(harness: Harness) -> None:
    _, other_key = make_pair(harness.tmp / "other", "other")
    run = harness.run(**harness.enabled(KTN_TLS_KEYFILE=str(other_key)))
    assert run.returncode == 1
    assert "ERROR: TLS is enabled" in run.output
    assert "invalid or mismatched" in run.output
    assert "KTN_TLS_ENABLED=false" in run.output  # the one-line recovery
    assert run.uvicorn_args is None, "must not fall back to plain HTTP"
    assert run.sleeps == 9  # ten attempts, one second apart
    assert "-----BEGIN" not in run.output and "PRIVATE KEY" not in run.output
    # The copies of the bad pair are removed, not left on the tmpfs.
    assert not (harness.runtime / "key.pem").exists()


def test_a_pair_that_settles_during_the_retries_is_accepted(harness: Harness) -> None:
    """TrueNAS writes cert then key: a start in between sees a mismatch once."""
    _, stale_key = make_pair(harness.tmp / "stale", "stale")
    live_key = harness.tmp / "live.key"
    shutil.copyfile(stale_key, live_key)
    run = harness.run(
        **harness.enabled(KTN_TLS_KEYFILE=str(live_key)),
        SHIM_ON_FIRST_SLEEP=f"cat '{harness.key}' > '{live_key}'",
    )
    assert run.returncode == 0, run.output
    assert run.sleeps == 1
    assert run.uvicorn_args is not None and "--ssl-keyfile" in run.uvicorn_args


def test_a_missing_key_names_the_path(harness: Harness) -> None:
    missing = harness.tmp / "absent.key"
    run = harness.run(**harness.enabled(KTN_TLS_KEYFILE=str(missing)))
    assert run.returncode == 1
    assert f"{missing} is unreadable" in run.output
    assert run.uvicorn_args is None


def test_enabled_without_paths_refuses(harness: Harness) -> None:
    run = harness.run(KTN_TLS_ENABLED="on")
    assert run.returncode == 1
    assert "KTN_TLS_CERTFILE and KTN_TLS_KEYFILE" in run.output
    assert run.uvicorn_args is None


@pytest.mark.parametrize("value", ["maybe", "t", "2", " true"])
def test_an_unknown_spelling_refuses(harness: Harness, value: str) -> None:
    run = harness.run(**harness.enabled(KTN_TLS_ENABLED=value))
    assert run.returncode == 1
    assert "is not one of" in run.output
    assert run.uvicorn_args is None


def non_tmpfs_dir() -> Path:
    """Some existing directory on a real disk filesystem, never written here.

    The checkout is the obvious one, but a contributor running the suite from
    /tmp (a tmpfs on many distros) would see this test fail for no product
    reason, so look further before giving up.
    """
    for candidate in (ROOT, Path("/var/tmp"), Path.home(), Path("/")):
        try:
            if candidate.is_dir() and fs_type(candidate) != "tmpfs":
                return candidate
        except (OSError, subprocess.CalledProcessError):
            continue
    pytest.fail("no non-tmpfs directory found (tried the checkout, /var/tmp, $HOME, /)")


def test_a_runtime_dir_off_tmpfs_refuses_before_copying(harness: Harness) -> None:
    on_disk = non_tmpfs_dir()
    assert fs_type(on_disk) != "tmpfs"
    target = on_disk / f"ktn-test-tls-{uuid.uuid4().hex}"
    assert not target.exists()
    run = harness.run(**harness.enabled(KTN_TLS_RUNTIME_DIR=str(target)))
    assert run.returncode == 1
    assert "is not a tmpfs" in run.output
    assert run.uvicorn_args is None
    assert not target.exists()


# ------------------------------------------------- the TLS marker (review M1)
# The app refuses to start when its own KTN_TLS_ENABLED disagrees with whether
# the entrypoint really started TLS (see test_tls_name_case.py). The
# entrypoint's half of that contract is KTN_TLS_ACTIVE=1, exported only once
# the --ssl-* arguments are built - and never inherited from the caller.


def test_tls_on_exports_the_marker_to_uvicorn(harness: Harness) -> None:
    run = harness.run(**harness.enabled())
    assert run.returncode == 0, run.output
    assert run.uvicorn_args is not None and "--ssl-certfile" in run.uvicorn_args
    assert run.tls_active == "1"


def test_tls_off_never_passes_a_marker_through(harness: Harness) -> None:
    """A stray KTN_TLS_ACTIVE in the compose must not claim TLS for a plain start."""
    run = harness.run(KTN_TLS_ACTIVE="1")
    assert run.returncode == 0, run.output
    assert run.uvicorn_args is not None
    assert not any(arg.startswith("--ssl") for arg in run.uvicorn_args)
    assert run.tls_active == "<unset>"


def test_a_lowercase_name_gives_plain_args_and_no_marker(harness: Harness) -> None:
    """What the entrypoint does with ktn_tls_enabled: plain HTTP, no marker -
    which is exactly what the app then refuses (test_tls_name_case.py)."""
    run = harness.run(
        ktn_tls_enabled="true",
        ktn_tls_certfile=str(harness.cert),
        ktn_tls_keyfile=str(harness.key),
    )
    assert run.returncode == 0, run.output
    assert run.uvicorn_args is not None
    assert not any(arg.startswith("--ssl") for arg in run.uvicorn_args)
    assert run.tls_active == "<unset>"
