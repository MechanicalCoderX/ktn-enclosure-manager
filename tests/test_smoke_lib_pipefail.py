"""The TLS smoke test must not read a found log line as a missing one.

tls-smoke-lib.sh sets pipefail. Under it, `docker logs c | grep -q needle`
fails when grep exits at the first match and docker dies of SIGPIPE on its next
write. On 2026-10-30 that failed the smoke test with "the watcher did not log
the renewal" while the line was in the log. The same pipeline in an
`if ...; then fail` leak check would pass with the leak present.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / ".github" / "tls-smoke-lib.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"

# Writes the match, then keeps writing after grep has had time to exit.
FAKE_DOCKER = "#!/bin/sh\necho needle\nsleep 0.3\necho later\nsleep 0.3\necho later\n"


def _bash(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"\n{script}'],
        capture_output=True, text=True, env=env, timeout=20, check=False,
    )


def test_control_grep_q_under_the_libs_pipefail_reports_a_found_line_as_missing(
    tmp_path: Path,
) -> None:
    result = _bash(tmp_path, "docker logs c 2>&1 | grep -q needle || exit 9\necho found")
    assert result.returncode == 9, (result.returncode, result.stdout)


def test_log_has_finds_a_line_while_the_writer_is_still_writing(tmp_path: Path) -> None:
    result = _bash(tmp_path, "log_has c needle || exit 9\necho found")
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout.strip() == "found"


def test_log_has_reports_an_absent_string(tmp_path: Path) -> None:
    result = _bash(tmp_path, "if log_has c absent; then exit 9; fi\necho absent")
    assert result.returncode == 0, (result.returncode, result.stderr)


def test_the_workflow_never_pipes_docker_logs_into_grep_q() -> None:
    text = WORKFLOW.read_text()
    assert "docker logs" in text  # control: the workflow still reads logs at all
    assert not re.findall(r"docker logs[^\n|]*\|\s*grep\s+-q", text)
