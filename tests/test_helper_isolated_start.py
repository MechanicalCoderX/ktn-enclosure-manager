"""The privileged helper starts isolated, as the image's HEALTHCHECK does.

docker-entrypoint.sh starts helper/ktn_ident_helper.py as root, once, at
container start. A normal Python start imports `site`, which executes every
.pth file and any sitecustomize it finds, and honours PYTHONPATH and the other
PYTHON* variables. A compromised web process can force that start whenever it
likes by exiting (the container restarts), so any bug that lets uid 1000
create a file root's Python reads at start-up is root code execution. M3's
dangling symlink was such a bug (REVIEW-M3 F3; M3-REPORT R2.6).

The helper therefore starts with `python -I -S` and sets its own import path
(helper/ktn_ident_helper.py, _import_path). Every test here reads the start
command from the entrypoint itself, so they fail if that line drifts.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pydantic
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
HELPER = ROOT / "helper" / "ktn_ident_helper.py"
IMAGE_HELPER = "/app/helper/ktn_ident_helper.py"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
FIXTURE_ENCLOSURE = "0x50060480aabbcc00"
VERIFY = ROOT / ".github" / "workflows" / "verify.yml"
SMOKE_LIB = ROOT / ".github" / "tls-smoke-lib.sh"
HELPER_SOCKET_ENV = "-e KTN_IDENT_HELPER_SOCKET=/run/ktn/ident.sock"


def helper_start_argv() -> list[str]:
    """The entrypoint's helper command, from the interpreter to the script."""
    text = re.sub(r"\\\n", " ", ENTRYPOINT.read_text())
    found = [
        line for line in text.splitlines()
        if IMAGE_HELPER in line and not line.lstrip().startswith("#")
    ]
    assert len(found) == 1, found
    argv = shlex.split(found[0])
    return argv[: argv.index(IMAGE_HELPER) + 1]


def interpreter_flags() -> list[str]:
    argv = helper_start_argv()
    assert argv[0] == "python" and argv[-1] == IMAGE_HELPER, argv
    return argv[1:-1]


def test_the_entrypoint_starts_the_helper_isolated() -> None:
    """Exact, so any drift in the line fails here first: a dropped flag, an
    added one, or an environment prefix in front of the interpreter."""
    assert helper_start_argv() == ["python", "-I", "-S", IMAGE_HELPER]


# ------------------------------------------------------------- end to end


@pytest.fixture
def site_plant(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A .pth in the site-packages the helper imports pydantic from.

    That is the directory a planted .pth would have to be in to matter: the
    helper's start cannot avoid reading it. The plant is inert unless
    KTN_TEST_PLANT_MARKER names a file, so other processes that start while
    it exists are unaffected, and it is removed afterwards.
    """
    site_dir = Path(pydantic.__file__).resolve().parent.parent
    if not os.access(site_dir, os.W_OK):
        pytest.skip(f"cannot plant a .pth: {site_dir} is not writable")
    pth = site_dir / f"zz-ktn-test-plant-{uuid.uuid4().hex}.pth"
    pth.write_text(
        "import os; os.environ.get('KTN_TEST_PLANT_MARKER') and "
        "open(os.environ['KTN_TEST_PLANT_MARKER'], 'a').write('pth\\n')\n"
    )
    marker = tmp_path / "ran"
    try:
        yield pth, marker
    finally:
        pth.unlink(missing_ok=True)


def start_helper(flags: list[str], tmp_path: Path, env: dict[str, str]) -> dict[str, object]:
    """Start the helper with ``flags``, ask it one question, stop it.

    The same arguments as the entrypoint, against a copy of the sysfs fixture.
    Returns the helper's answer to an identify_read, which needs the backend
    (ktnmgr) and its third-party dependencies to have been imported.
    """
    work = tmp_path / f"run-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    sysfs = work / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    sock = work / "ident.sock"
    log_path = work / "helper.log"
    argv = [
        sys.executable, *flags, str(HELPER),
        "--socket", str(sock),
        "--socket-group", str(os.getegid()),
        "--sysfs-root", str(sysfs),
        "--allow", "",
        "--ident-method", "sysfs",
        "--enclosure-lock", str(work / "enclosure.lock"),
    ]
    with log_path.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=work, env=env, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 15
        while not sock.is_socket():
            if proc.poll() is not None:
                raise AssertionError(f"helper exited early:\n{log_path.read_text()}")
            if time.monotonic() > deadline:
                raise AssertionError(f"helper socket never appeared:\n{log_path.read_text()}")
            time.sleep(0.05)
        request = {"op": "identify_read", "enclosure_id": FIXTURE_ENCLOSURE, "slot": 0}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(sock))
            client.sendall((json.dumps(request) + "\n").encode())
            answer: dict[str, object] = json.loads(client.makefile("rb").readline())
        return answer
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_planted_start_up_code_does_not_run_in_the_helper(
    site_plant: tuple[Path, Path], tmp_path: Path
) -> None:
    """The helper started the way the entrypoint starts it serves requests,
    and runs neither a .pth in its own site-packages nor a sitecustomize
    reachable through PYTHONPATH. The controls show both plants are live."""
    _, marker = site_plant
    plant = tmp_path / "pythonpath"
    plant.mkdir()
    (plant / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'a').write('sitecustomize\\n')\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("KTN_")}
    env.update(PYTHONPATH=os.pathsep.join([str(plant), str(ROOT / "backend")]),
               KTN_TEST_PLANT_MARKER=str(marker))

    def ran() -> list[str]:
        # A set: some layouts (Debian's venvs) process the same .pth twice.
        found = sorted(set(marker.read_text().split())) if marker.exists() else []
        marker.unlink(missing_ok=True)
        return found

    # Control: a plain start, as 1.6.3's entrypoint did, runs both.
    assert start_helper([], tmp_path, env) == {"ok": True, "locate": False}
    assert ran() == ["pth", "sitecustomize"], "control: a plant did not run"

    # Control: -I alone shuts out PYTHONPATH but still runs site, so the .pth.
    assert start_helper(["-I"], tmp_path, env) == {"ok": True, "locate": False}
    assert ran() == ["pth"], "control: -I alone should still process .pth files"

    # As the entrypoint starts it.
    assert start_helper(interpreter_flags(), tmp_path, env) == {"ok": True, "locate": False}
    assert ran() == [], "the helper's root start executed planted start-up code"


def test_the_isolated_helper_imports_only_from_its_own_directories() -> None:
    """Under the entrypoint's flags, importing the helper adds the backend
    beside it and the interpreter's site-packages to sys.path, and nothing
    else; `site` never runs. The backend comes first, as PYTHONPATH put it."""
    probe = (
        "import importlib.util, json, sys\n"
        "before = list(sys.path)\n"
        f"spec = importlib.util.spec_from_file_location('h', {str(HELPER)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(json.dumps({'before': before, 'after': sys.path,\n"
        "    'no_site': sys.flags.no_site, 'isolated': sys.flags.isolated,\n"
        "    'site_ran': 'sitecustomize' in sys.modules or hasattr(sys, '__interactivehook__'),\n"
        "    'pydantic': sys.modules['pydantic'].__file__,\n"
        "    'ktnmgr': sys.modules['ktnmgr'].__file__}))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("KTN_")}
    out = subprocess.run(
        [sys.executable, *interpreter_flags(), "-c", probe],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout)
    assert seen["no_site"] == 1 and seen["isolated"] == 1
    assert not seen["site_ran"]

    backend = str(ROOT / "backend")
    site_dir = str(Path(pydantic.__file__).resolve().parent.parent)
    after, before = seen["after"], seen["before"]
    assert after[0] == backend
    added = [p for p in after if p not in before]
    resolved = {str(Path(p).resolve()) for p in added}
    assert resolved == {str(Path(backend).resolve()), site_dir}, added
    assert Path(seen["ktnmgr"]).resolve().is_relative_to(Path(backend).resolve())
    assert Path(seen["pydantic"]).resolve().is_relative_to(Path(site_dir))


def test_the_isolated_helper_needs_its_import_path() -> None:
    """Control for the test above: with the same flags, without the helper's
    own path setup, the backend's third-party dependency is not importable.
    So the helper really depends on _import_path, not on luck."""
    probe = (
        f"import sys; sys.path.insert(0, {str(ROOT / 'backend')!r})\n"
        "import ktnmgr.enclosure.sysfs\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c", probe],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert out.returncode != 0
    assert "No module named 'pydantic'" in out.stderr, out.stderr


# ----------------------------------------------- the image, in CI (REVIEW-164 F4)


def test_a_ci_smoke_container_starts_the_real_helper() -> None:
    """The tests above start the helper on CI's interpreter. Only a container
    proves the image's own interpreter and layout (the non-venv sysconfig
    branch, /usr/local, pip's site-packages) give it what it imports, so one
    smoke container in verify.yml must configure the helper and check that
    /healthz reports it ok - on the /run/ktn tmpfs, as the app runs it."""
    steps = yaml.safe_load(VERIFY.read_text())["jobs"]["tls-smoke"]["steps"]
    runs = [re.sub(r"\\\n\s*", " ", step.get("run", "")) for step in steps]
    docker_runs = [
        (run, line) for run in runs for line in run.splitlines()
        if line.strip().startswith("docker run ")
    ]
    assert len(docker_runs) >= 3, "control: the job's docker run lines were found"

    with_helper = [(run, line) for run, line in docker_runs if HELPER_SOCKET_ENV in line]
    assert len(with_helper) == 1, "no smoke container starts the IDENT helper"
    run, line = with_helper[0]
    assert '"${HARDENING[@]}"' in line, "the helper's socket needs HARDENING's /run/ktn tmpfs"
    assert "--tmpfs /run/ktn:" in SMOKE_LIB.read_text()
    argv = shlex.split(line)
    name = argv[argv.index("--name") + 1]
    assert f"wait_healthy {name}" in run
    # Captured, then searched (tests/test_smoke_lib_pipefail.py).
    assert "grep -qF '\"helper\":\"ok\"' <<<\"$health\"" in run


@pytest.mark.parametrize("cfg_at", ["bin", "root", None])
def test_a_venv_is_recognised_without_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg_at: str | None
) -> None:
    """_import_path's pyvenv.cfg branch (REVIEW-164 N5), on any interpreter.

    Under -S nothing switches a venv to its own prefix, so the helper looks
    for pyvenv.cfg beside the interpreter or one level up, as the interpreter
    does. On CI's 3.15 getpath already sets sys.prefix to the venv, so the
    subprocess tests above pass with or without this branch; here a fake venv
    layout, an interpreter path inside it and nothing else decide.
    """
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    if cfg_at == "bin":
        (venv / "bin" / "pyvenv.cfg").write_text("home = /nonexistent\n")
    elif cfg_at == "root":
        (venv / "pyvenv.cfg").write_text("home = /nonexistent\n")

    monkeypatch.setattr(sys, "path", list(sys.path))  # the module extends it on load
    spec = importlib.util.spec_from_file_location("ktn_ident_helper_venv_probe", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "executable", str(venv / "bin" / "python"))

    backend, *packages = module._import_path()

    assert backend == str(ROOT / "backend")
    inside = [Path(p).is_relative_to(venv) for p in packages]
    if cfg_at is None:
        # Control: no pyvenv.cfg, so this interpreter's own install paths.
        assert inside == [False, False], packages
    else:
        assert inside == [True, True], packages
