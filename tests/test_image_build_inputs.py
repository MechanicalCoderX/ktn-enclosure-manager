"""What the image build installs, and whether a failure there can be hidden.

Three properties of the Dockerfile that no other test reads:

- A failed ``pip install`` must fail the build. ``A && B || true`` succeeds
  whenever A fails, so an image with no fastapi or uvicorn would pass the
  build and the Trivy scan and be published (deep review M4).
- The runtime dependencies are a hash-pinned lock, not whatever PyPI serves on
  the day of the build. Without it a "code-identical" release is not
  dependency-identical, and a rollback is also a dependency rollback (L1).
- The BuildKit frontend named on line 1 is executed by the publish job, which
  holds a packages:write token; like the base images it is pinned by digest
  (L4).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
BACKEND = ROOT / "backend"
LOCK = BACKEND / "requirements.txt"
LOCK_INPUT = BACKEND / "requirements.in"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


def _instructions() -> list[str]:
    """Dockerfile instructions with continuation lines joined; comments dropped."""
    out: list[str] = []
    current = ""
    for raw in DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        out.append(current + line)
        current = ""
    return out


def _pip_run_command() -> str:
    runs = [i for i in _instructions() if i.startswith("RUN ") and "pip install" in i]
    assert len(runs) == 1, f"expected one RUN with pip install, found {len(runs)}"
    return runs[0][len("RUN ") :]


def _run_with_fake_pip(tmp_path: Path, fail_on: str) -> subprocess.CompletedProcess[str]:
    """Run the Dockerfile's pip RUN line under /bin/sh with a scripted pip.

    The fake exits 1 when its first argument is ``fail_on`` and 0 otherwise,
    and logs each call, so the test sees the real shell precedence of the line
    as written rather than a re-statement of it.
    """
    log = tmp_path / "calls.log"
    fake = tmp_path / "pip"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        f'[ "$1" = "{fail_on}" ] && exit 1\n'
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["/bin/sh", "-c", _pip_run_command()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# ------------------------------------------------------------------ M4


def test_failed_pip_install_fails_the_build(tmp_path: Path) -> None:
    result = _run_with_fake_pip(tmp_path, fail_on="install")
    assert result.returncode != 0, "a failing `pip install` exits 0: the build would succeed"


def test_failed_dependency_check_fails_the_build(tmp_path: Path) -> None:
    result = _run_with_fake_pip(tmp_path, fail_on="check")
    assert result.returncode != 0, "a failing `pip check` exits 0"


def test_only_the_pip_uninstall_may_fail(tmp_path: Path) -> None:
    """Control for the two tests above: the line is not simply `set -e`-strict.

    Removing pip is best-effort by design, so its failure must not fail the
    build - and every step must still have run.
    """
    result = _run_with_fake_pip(tmp_path, fail_on="uninstall")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["install", "install", "check", "uninstall"], calls


# ------------------------------------------------------------------ L1


def _lock_pins() -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hashes) for every requirement in the lock."""
    pins: dict[str, tuple[str, list[str]]] = {}
    text = LOCK.read_text()
    # One logical requirement per entry: join the backslash continuations.
    for entry in re.sub(r"\\\n", " ", text).splitlines():
        entry = entry.split("#", 1)[0].strip()
        if not entry:
            continue
        spec, _, _ = entry.partition(" --hash")
        req = Requirement(spec.split(" ;")[0].strip())
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", f"not an exact pin: {spec}"
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", entry)
        pins[canonicalize_name(req.name)] = (specs[0].version, hashes)
    return pins


def test_lock_exists_and_every_pin_is_hashed() -> None:
    assert LOCK.is_file(), "backend/requirements.txt (the image's dependency lock) is missing"
    pins = _lock_pins()
    assert len(pins) >= 8, pins
    unhashed = [name for name, (_, hashes) in pins.items() if not hashes]
    assert not unhashed, f"pins without hashes: {unhashed}"


def test_lock_input_is_pyprojects_runtime_dependencies() -> None:
    """requirements.in is a copy of pyproject's list; pyproject stays the source.

    Dependabot regenerates a compiled lock only from a .in file, so the list
    has to exist twice. This test is what keeps the two from drifting.
    """
    declared = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]["dependencies"]
    lines = [
        line.split("#", 1)[0].strip()
        for line in LOCK_INPUT.read_text().splitlines()
    ]
    listed = [line for line in lines if line]
    assert sorted(str(Requirement(r)) for r in listed) == sorted(
        str(Requirement(r)) for r in declared
    )


def test_lock_satisfies_every_declared_dependency() -> None:
    declared = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]["dependencies"]
    pins = _lock_pins()
    for raw in declared:
        req = Requirement(raw)
        name = canonicalize_name(req.name)
        assert name in pins, f"{req.name} is declared but not in the lock"
        version = pins[name][0]
        assert req.specifier.contains(version, prereleases=True), f"{name}=={version} vs {req}"


def test_lock_targets_the_image_python_on_every_platform() -> None:
    """The lock is resolved for the interpreter the image runs, on any arch.

    --universal keeps one lock valid for both published architectures, and
    --generate-hashes records every file of each pinned version (the aarch64
    wheels included), so the arm64 leg verifies against the same lock.
    """
    runtime = re.search(r"^FROM python:(\d+\.\d+)-slim@sha256:", DOCKERFILE.read_text(), re.MULTILINE)
    assert runtime, "runtime base image not found"
    lines = LOCK.read_text().splitlines()
    header = "\n".join(line for line in lines[: next(i for i, x in enumerate(lines) if not x.startswith("#"))])
    assert "uv pip compile" in header, header
    assert "--universal" in header
    assert "--generate-hashes" in header
    match = re.search(r"--python-version[=\s]+(\d+\.\d+)", header)
    assert match, "lock header records no --python-version"
    assert match.group(1) == runtime.group(1), (
        f"lock compiled for Python {match.group(1)}, image runs {runtime.group(1)}: "
        "regenerate the lock (CONTRIBUTING.md)"
    )


def test_image_installs_the_lock_with_hashes_required() -> None:
    cmd = _pip_run_command()
    install_lock = re.search(r"pip install [^&|;]*--require-hashes[^&|;]*-r /app/backend/requirements\.txt", cmd)
    assert install_lock, cmd
    # The project itself goes in after, without resolving anything new: a
    # dependency it pulled in here would bypass the lock.
    assert re.search(r"pip install [^&|;]*--no-deps[^&|;]*/app/backend(\s|$)", cmd), cmd


def test_dependabot_regenerates_the_lock_for_the_image_python() -> None:
    """The uv ecosystem, not pip, owns /backend.

    Dependabot's pip ecosystem re-runs pip-compile under the lowest
    pre-installed Python satisfying requires-python (>=3.11, so 3.11), and has
    no --universal. Its uv ecosystem reads --python-version and --universal
    back from the lock header.
    """
    updates = yaml.safe_load(DEPENDABOT.read_text())["updates"]
    backend = [u for u in updates if u["directory"].rstrip("/") == "/backend"]
    assert [u["package-ecosystem"] for u in backend] == ["uv"], backend


# ------------------------------------------------------------------ L4


def test_syntax_frontend_is_pinned_by_digest() -> None:
    first = DOCKERFILE.read_text().splitlines()[0]
    assert re.fullmatch(
        r"# syntax=docker/dockerfile:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", first
    ), first


def test_every_from_is_digest_pinned() -> None:
    """Control for the syntax test: the policy it extends already holds."""
    froms = re.findall(r"^FROM (\S+)", DOCKERFILE.read_text(), re.MULTILINE)
    assert froms
    assert all("@sha256:" in f for f in froms), froms
