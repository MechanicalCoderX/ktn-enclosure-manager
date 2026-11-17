"""docker-healthcheck.py follows KTN_TLS_ENABLED and KTN_PORT.

It must pass against a TLS instance when told TLS is on, and FAIL against
that same instance when told it is off - otherwise a passing result would
not show it had probed anything. Against a plain instance with TLS off it
passes, which is the unchanged default deployment.

Every probe runs the script with the interpreter flags the Dockerfile's
HEALTHCHECK uses, read from the Dockerfile itself, so these tests fail if
the script stops working the way the image actually runs it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tls_certs import ROOT, free_port, make_pair, run_server

SCRIPT = ROOT / "docker-healthcheck.py"


def healthcheck_argv() -> list[str]:
    """The HEALTHCHECK command from the Dockerfile, shell or exec form."""
    text = re.sub(r"\\\n", " ", (ROOT / "Dockerfile").read_text())
    found = [line for line in text.splitlines() if line.lstrip().startswith("HEALTHCHECK")]
    assert len(found) == 1, found
    command = found[0].split(" CMD ", 1)[1].strip()
    return json.loads(command) if command.startswith("[") else shlex.split(command)


def interpreter_flags() -> list[str]:
    argv = healthcheck_argv()
    assert argv[0] == "python" and argv[-1] == "/app/docker-healthcheck.py", argv
    return argv[1:-1]


def probe(port: int | None, tls: str | None) -> int:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KTN_")}
    if port is not None:
        env["KTN_PORT"] = str(port)
    if tls is not None:
        env["KTN_TLS_ENABLED"] = tls
    return subprocess.run(
        [sys.executable, *interpreter_flags(), str(SCRIPT)],
        env=env, capture_output=True, timeout=30, check=False,
    ).returncode


@pytest.fixture(scope="module")
def tls_port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    tmp = tmp_path_factory.mktemp("hc-tls")
    cert, key = make_pair(tmp / "pair")
    # As the container runs it: TLS on in the app's settings as well as on the
    # listener (the app refuses to start when the two disagree).
    env = {"KTN_TLS_ENABLED": "true", "KTN_TLS_CERTFILE": str(cert),
           "KTN_TLS_KEYFILE": str(key)}
    with run_server(tmp, cert=cert, key=key, env=env) as port:
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


# ------------------------------------------------- run isolated, as root runs it
#
# Docker runs the HEALTHCHECK as root every 30 s (no USER in the image, and the
# catalog sets user 0). With normal start-up, root's Python imports `site`,
# which executes every .pth file and any sitecustomize in site-packages - so
# any bug that lets a lesser process create a file there becomes root code
# execution within one interval. On 1549236 that is what M3's dangling symlink
# gave the web process (REVIEW-M3 F3, shown in a real container).


def test_the_image_runs_the_healthcheck_isolated() -> None:
    """-I: no PYTHON* environment, no user site, no script directory on the
    path. -S: no `site` at all, so no site-packages, .pth or sitecustomize."""
    flags = interpreter_flags()
    assert "-I" in flags and "-S" in flags, flags

    check = "import sys; print(sys.flags.isolated, sys.flags.no_site, 'site' in sys.modules)"
    isolated = subprocess.run(
        [sys.executable, *flags, "-c", check], capture_output=True, text=True, check=True
    ).stdout.split()
    assert isolated == ["1", "1", "False"], isolated
    # Control: the same interpreter without the flags does import site.
    plain = subprocess.run(
        [sys.executable, "-c", check], capture_output=True, text=True, check=True
    ).stdout.split()
    assert plain == ["0", "0", "True"], plain


def test_a_planted_sitecustomize_does_not_run_in_the_healthcheck(
    plain_port: int, tmp_path: Path
) -> None:
    """End to end: the real script, run with the image's flags, still passes
    and does not execute start-up code it finds on the way. PYTHONPATH stands
    in for the planted location; the control shows the plant would run."""
    plant = tmp_path / "plant"
    plant.mkdir()
    marker = tmp_path / "ran"
    (plant / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'w').write('executed')\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("KTN_")}
    env.update(KTN_PORT=str(plain_port), PYTHONPATH=str(plant))

    def run(flags: list[str]) -> int:
        return subprocess.run(
            [sys.executable, *flags, str(SCRIPT)],
            env=env, capture_output=True, timeout=30, check=False,
        ).returncode

    assert run([]) == 0
    assert marker.exists(), "control: the plant did not run without isolation"
    marker.unlink()

    assert run(interpreter_flags()) == 0, "the healthcheck fails when run isolated"
    assert not marker.exists(), "the healthcheck executed a planted sitecustomize"
