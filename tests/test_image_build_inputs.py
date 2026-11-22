"""What the image build installs, and whether a failure there can be hidden.

Three properties of the Dockerfile that no other test reads:

- A failed ``pip install`` must fail the build. ``A && B || true`` succeeds
  whenever A fails, so an image with no fastapi or uvicorn would pass the
  build and the Trivy scan and be published (deep review M4).
- The runtime dependencies are a hash-pinned lock, not whatever PyPI serves on
  the day of the build. Without it a "code-identical" release is not
  dependency-identical, and a rollback is also a dependency rollback (L1).
  The lock installs from wheels only, so no build backend runs for it, and
  it names every runtime package (no extras), so `pip check` sees them all.
- The BuildKit frontend named on line 1 is executed by the publish job, which
  holds a packages:write token; like the base images it is pinned by digest
  (L4).

And three properties of CI: the suite, the E2E run and the audit install that
same lock, so they check the library versions the image ships rather than the
newest ones pyproject's ranges allow; the lock is audited as a file; and the
lock's body is re-derived from its inputs on every run.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
BACKEND = ROOT / "backend"
LOCK = BACKEND / "requirements.txt"
LOCK_INPUT = BACKEND / "requirements.in"
BUILD_LOCK = BACKEND / "build-requirements.txt"
BUILD_LOCK_INPUT = BACKEND / "build-requirements.in"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
VERIFY = ROOT / ".github" / "workflows" / "verify.yml"
LOCK_CHECK = ROOT / "scripts" / "check-lock.sh"


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


# `pip install`, `pip3 install`, `python -m pip install`, `pip3.15 install`...
_PIP_INSTALL = re.compile(r"\bpip[\d.]*\s+install\b")


def _pip_run_command() -> str:
    runs = [i for i in _instructions() if i.startswith("RUN ") and _PIP_INSTALL.search(i)]
    assert len(runs) == 1, f"expected one RUN that installs Python packages, found {runs}"
    return runs[0][len("RUN ") :]


def _run_with_fake_pip(tmp_path: Path, fail_call: int) -> subprocess.CompletedProcess[str]:
    """Run the Dockerfile's pip RUN line under /bin/sh with a scripted pip.

    The fake exits 1 on its ``fail_call``-th invocation and 0 otherwise, and
    logs each call, so the test sees the real shell precedence of the line as
    written rather than a re-statement of it. It fails by position, not by
    subcommand: the line runs `install` twice, and a fake that failed on any
    `install` would never reach the second one.
    """
    log = tmp_path / "calls.log"
    count = tmp_path / "count"
    fake = tmp_path / "pip"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        f'n=$(( $(cat "{count}" 2>/dev/null || echo 0) + 1 ))\n'
        f'echo "$n" > "{count}"\n'
        f'[ "$n" -eq {fail_call} ] && exit 1\n'
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


def _calls(tmp_path: Path) -> list[str]:
    return [c.split()[0] for c in (tmp_path / "calls.log").read_text().splitlines()]


# ------------------------------------------------------------------ M4


@pytest.mark.parametrize(
    ("fail_call", "step"),
    [(1, "install"), (2, "install"), (3, "install"), (4, "uninstall"), (5, "check")],
    ids=["lock-install", "build-lock-install", "project-install", "setuptools-uninstall", "pip-check"],
)
def test_a_failed_step_fails_the_build(tmp_path: Path, fail_call: int, step: str) -> None:
    result = _run_with_fake_pip(tmp_path, fail_call)
    assert result.returncode != 0, f"pip call {fail_call} (`pip {step}`) failed; the line exited 0"
    calls = _calls(tmp_path)
    assert calls[fail_call - 1] == step, calls
    assert len(calls) == fail_call, f"the line carried on after a failed step: {calls}"


def test_only_the_pip_uninstall_may_fail(tmp_path: Path) -> None:
    """Control for the test above: the line is not simply `set -e`-strict.

    Removing pip is best-effort by design, so its failure must not fail the
    build - and every step must still have run.
    """
    result = _run_with_fake_pip(tmp_path, fail_call=6)
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == [
        "install", "install", "install", "uninstall", "check", "uninstall",
    ]


def test_no_other_run_installs_python_packages() -> None:
    """The guarded line is the only way into site-packages.

    It is located by its `pip install`, so a second, unguarded install (say
    `RUN pip3 install requests`) would otherwise go unseen by every test here.
    """
    runs = [i for i in _instructions() if i.startswith("RUN ") and re.search(r"\bpip", i)]
    assert len(runs) == 1, runs


# ------------------------------------------------------------------ L1


def _lock_pins(lock: Path = LOCK) -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hashes) for every requirement in the lock."""
    pins: dict[str, tuple[str, list[str]]] = {}
    text = lock.read_text()
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


def _lock_header(lock: Path = LOCK) -> str:
    lines = lock.read_text().splitlines()
    return "\n".join(lines[: next(i for i, x in enumerate(lines) if not x.startswith("#"))])


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
    header = _lock_header()
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


def _declared() -> list[Requirement]:
    deps = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]["dependencies"]
    return [Requirement(r) for r in deps]


def test_image_lock_install_is_wheels_only() -> None:
    """No pinned dependency is built from source inside the image.

    The hashes pin an sdist's bytes, but building it runs its build backend
    (for PyYAML, before ``uvicorn[standard]`` left: setuptools and a native
    Cython wheel), which pip fetches unhashed, newest at build time, as root
    in the build container - and the wheel it produces ships. Every pin has a wheel for the image's
    Python on both architectures, so --only-binary turns a future sdist-only
    pin into a failed build instead of a silent source build.
    """
    cmd = _pip_run_command()
    lock_install = [s for s in cmd.split("&&") if "-r /app/backend/requirements.txt" in s]
    assert len(lock_install) == 1, cmd
    assert re.search(r"--only-binary[ =]:all:(\s|$)", lock_install[0]), lock_install[0]
    # Present is not the same as in effect: a --no-binary on the same line
    # (":all:", or the names it lists) switches source builds back on, and
    # PIP_NO_BINARY does the same from the environment. REVIEW-164 N6: the
    # mutant `--only-binary :all: --no-binary :all:` passed this test.
    assert "--no-binary" not in cmd, cmd
    assert not [i for i in _instructions() if "PIP_NO_BINARY" in i]


def test_runtime_dependencies_use_no_extras() -> None:
    """Every runtime package is named, so the image's `pip check` sees it.

    `pip check` ignores extras: with ``uvicorn[standard]`` declared, a lock
    missing uvloop or httptools installed and checked clean, and uvicorn then
    fell back to asyncio and h11 without a word. A plain requirement is seen
    by `pip check`, by the lock tests above and by the drift test.
    """
    with_extras = [str(r) for r in _declared() if r.extras]
    assert not with_extras, f"declare the packages an extra brings directly: {with_extras}"


def test_the_server_stack_is_declared_directly() -> None:
    """What ``uvicorn[standard]`` brought that the app uses, by name.

    uvloop and httptools are uvicorn's event loop and HTTP parser; uvicorn
    picks them when installed (``--loop auto``, ``--http auto``). websockets is
    the TrueNAS JSON-RPC client's transport (ktnmgr/truenas/client.py), and
    what uvicorn's ``--ws auto`` picks. python-dotenv comes in as a hard
    requirement of pydantic-settings (``env_file=".env"`` in config.py).
    Left out on purpose: watchfiles (``--reload`` only) and PyYAML (a YAML
    ``--log-config`` only, and the one pin with no cp315 wheel).
    """
    names = {canonicalize_name(r.name) for r in _declared()}
    assert {"uvicorn", "uvloop", "httptools", "websockets"} <= names, names
    pins = _lock_pins()
    assert {"uvloop", "httptools", "websockets", "python-dotenv"} <= pins.keys(), sorted(pins)
    assert not {"pyyaml", "watchfiles"} & pins.keys(), sorted(pins)


@pytest.mark.skipif(sys.platform == "win32", reason="uvloop does not support Windows")
def test_uvicorn_auto_selects_uvloop_and_httptools() -> None:
    """Installed from these dependencies, uvicorn's ``auto`` choices are the
    same implementations ``[standard]`` gave: httptools and uvloop.
    """
    from uvicorn.protocols.http.auto import AutoHTTPProtocol
    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

    assert AutoHTTPProtocol is HttpToolsProtocol
    assert importlib.util.find_spec("uvloop") is not None, "uvloop is not installed"


def test_dependabot_regenerates_the_lock_for_the_image_python() -> None:
    """The uv ecosystem, not pip, owns /backend.

    Dependabot's uv ecosystem reads --universal, --python-version and
    --generate-hashes back from the lock header and recompiles with them; its
    pip ecosystem runs pip-compile with no --universal, and would rewrite the
    lock for one platform only.

    For the direct dependencies declared in pyproject.toml, uv picks the
    *target version* under the lowest Python requires-python allows; that
    floor is held to the image's Python by test_version_consistency.py.
    """
    updates = yaml.safe_load(DEPENDABOT.read_text())["updates"]
    backend = [u for u in updates if u["directory"].rstrip("/") == "/backend"]
    assert [u["package-ecosystem"] for u in backend] == ["uv"], backend


# ------------------------------------------------------------------ CI


def _job_runs(job: str) -> list[str]:
    jobs = yaml.safe_load(VERIFY.read_text())["jobs"]
    return [step["run"] for step in jobs[job]["steps"] if "run" in step]


def _compile_options(text: str) -> set[str]:
    """The options of the `pip compile` command in ``text``, values attached."""
    text = re.sub(r"\\\n", " ", text)  # shell line continuations
    match = re.search(r"pip compile((?:\s+--[a-z][a-z-]*(?:[ =]\d+\.\d+)?)+)", text)
    assert match, "no `pip compile` command found"
    return set(re.findall(r"--[a-z][a-z-]*(?:[ =]\d+\.\d+)?", match.group(1)))


@pytest.mark.parametrize("job", ["backend", "e2e"])
def test_ci_tests_the_locked_dependency_set(job: str) -> None:
    """The suite and E2E run on the versions the image ships.

    The lock is installed first with its hashes; the project and its dev tools
    then go in constrained to what is already installed, so resolving them
    cannot move a runtime pin to the newest release pyproject allows.
    """
    script = "\n".join(_job_runs(job))
    lock = re.search(r"pip install --require-hashes -r backend/requirements\.txt", script)
    project = re.search(r"pip install (.*)-e 'backend\[dev\]'", script)
    assert lock, f"{job}: the lock is not installed"
    assert project, f"{job}: the project is not installed"
    assert lock.start() < project.start(), f"{job}: the project goes in before the lock"
    assert re.search(r"(^|\s)-c\s", project.group(1)), (
        f"{job}: the project install is not constrained to the locked set"
    )


def test_ci_audits_the_lock_itself() -> None:
    audits = [line for run in _job_runs("backend") for line in run.splitlines() if "pip-audit " in line]
    assert any("-r backend/requirements.txt" in a for a in audits), audits


def test_ci_rederives_the_lock_from_its_inputs() -> None:
    """A lock edited by hand, or compiled differently, fails CI.

    The header-only test above cannot see the body: a lock whose header says
    --universal --python-version 3.15 over a body compiled for 3.11 on one
    platform passes it. The script recompiles with the lock's own pins as
    constraints and compares bodies, and must use the documented options.
    """
    assert any("scripts/check-lock.sh" in run for run in _job_runs("backend"))
    assert LOCK_CHECK.is_file()
    documented = _compile_options(_lock_header())
    assert {"--universal", "--generate-hashes"} <= documented, documented
    assert documented <= _compile_options(LOCK_CHECK.read_text()), (
        "scripts/check-lock.sh does not compile with the lock header's options"
    )


# ------------------------------------------------------- build backend lock


def _build_requires() -> list[str]:
    return tomllib.loads((BACKEND / "pyproject.toml").read_text())["build-system"]["requires"]


def _steps(cmd: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\|", cmd.replace("(", " ").replace(")", " "))]


def test_build_lock_input_is_pyprojects_build_requires() -> None:
    """build-requirements.in is a copy of [build-system].requires.

    Same reason as requirements.in: Dependabot recompiles a lock only from a
    .in file. This is what keeps the two from drifting.
    """
    assert BUILD_LOCK_INPUT.is_file(), "backend/build-requirements.in is missing"
    lines = [line.split("#", 1)[0].strip() for line in BUILD_LOCK_INPUT.read_text().splitlines()]
    listed = [line for line in lines if line]
    assert listed, "build-requirements.in lists nothing"
    assert sorted(str(Requirement(r)) for r in listed) == sorted(
        str(Requirement(r)) for r in _build_requires()
    )


def test_build_lock_exists_hashed_and_satisfies_build_requires() -> None:
    assert BUILD_LOCK.is_file(), "backend/build-requirements.txt (the build backend's lock) is missing"
    pins = _lock_pins(BUILD_LOCK)
    unhashed = [name for name, (_, hashes) in pins.items() if not hashes]
    assert not unhashed, f"pins without hashes: {unhashed}"
    for raw in _build_requires():
        req = Requirement(raw)
        name = canonicalize_name(req.name)
        assert name in pins, f"{req.name} is a build requirement but not in the build lock"
        assert req.specifier.contains(pins[name][0], prereleases=True), f"{name} vs {req}"


def test_build_lock_targets_the_image_python_on_every_platform() -> None:
    header = _lock_header(BUILD_LOCK)
    assert "uv pip compile" in header, header
    assert "--universal" in header
    assert "--generate-hashes" in header
    runtime = re.search(r"^FROM python:(\d+\.\d+)-slim@sha256:", DOCKERFILE.read_text(), re.MULTILINE)
    assert runtime
    match = re.search(r"--python-version[=\s]+(\d+\.\d+)", header)
    assert match and match.group(1) == runtime.group(1), header


def test_image_builds_the_project_with_the_hashed_backend() -> None:
    """setuptools comes from the hash-checked build lock, not an isolated env.

    Without --no-build-isolation pip fetches [build-system].requires itself,
    unhashed, newest at build time, as root; the wheel it builds then ships.
    """
    steps = _steps(_pip_run_command())
    lock = [s for s in steps if "-r /app/backend/build-requirements.txt" in s]
    assert len(lock) == 1, steps
    assert "--require-hashes" in lock[0]
    assert re.search(r"--only-binary[ =]:all:(\s|$)", lock[0]), lock[0]
    project = [s for s in steps if re.search(r"pip install\b.*\s/app/backend$", s)]
    assert len(project) == 1, steps
    assert "--no-build-isolation" in project[0]
    assert "--no-deps" in project[0]
    order = [steps.index(lock[0]), steps.index(project[0])]
    assert order == sorted(order), "the project is built before its backend is installed"


def test_image_removes_the_build_backend_before_pip() -> None:
    steps = _steps(_pip_run_command())
    gone = [i for i, s in enumerate(steps) if re.search(r"pip uninstall\b.*\bsetuptools\b", s)]
    project = next(i for i, s in enumerate(steps) if re.search(r"pip install\b.*\s/app/backend$", s))
    pip_gone = next(i for i, s in enumerate(steps) if re.search(r"pip uninstall\b.*\bpip\b", s))
    assert len(gone) == 1, steps
    assert project < gone[0] < pip_gone, steps
    # Strict: unlike removing pip, a setuptools that stayed is a failed build.
    assert "|| true" not in steps[gone[0]]


def test_ci_rederives_the_build_lock_from_its_input() -> None:
    script = LOCK_CHECK.read_text()
    assert "build-requirements.in" in script and "build-requirements.txt" in script
    assert _compile_options(_lock_header(BUILD_LOCK)) <= _compile_options(script)


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


def test_syntax_frontend_matches_the_stage_dependabot_bumps() -> None:
    """Line 1 cannot go stale silently.

    Dependabot's docker ecosystem reads only FROM lines, so the `syntax-pin`
    stage (never built: nothing copies from it and it is not the target)
    carries the same reference where Dependabot sees it. Its PR bumps the
    stage alone and fails here until line 1 is copied across.
    """
    first = DOCKERFILE.read_text().splitlines()[0]
    tracked = re.search(r"^FROM (docker/dockerfile:\S+) AS syntax-pin$", DOCKERFILE.read_text(), re.MULTILINE)
    assert tracked, "no `FROM docker/dockerfile:... AS syntax-pin` stage"
    assert first == f"# syntax={tracked.group(1)}", (
        f"line 1 is {first!r} but the Dependabot-tracked stage is {tracked.group(1)!r}: "
        "copy the stage's version and digest onto line 1"
    )
    assert not re.search(r"--from=syntax-pin\b", DOCKERFILE.read_text()), "syntax-pin must not be used"


def test_arm64_image_is_started_not_just_scanned() -> None:
    """The arm64 leg of the image job runs the image and checks its arch.

    Trivy never executes anything; without this step a broken arm64 runtime
    passes the gate and ships.
    """
    steps = yaml.safe_load(VERIFY.read_text())["jobs"]["image"]["steps"]
    runs = [s for s in steps if "ktn-enclosure-manager:ci" in s.get("run", "")]
    assert len(runs) == 1, "expected one step that runs the :ci image"
    step = runs[0]
    assert step["if"] == "matrix.arch == 'arm64'"
    assert "docker run" in step["run"]
    assert "/healthz" in step["run"]
    assert "aarch64" in step["run"]
