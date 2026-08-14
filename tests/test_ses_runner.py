"""sg_ses invocation shape.

The argv these assert on is not cosmetic. sg_ses 2.86 issues a REPORT
TIMESTAMP command on every run unless told not to; the KTN-STL3 rejects it,
the HBA returns DID_SOFT_ERROR and mpt3sas logs an abort. That was ~5,700
kernel messages a day on the validation system, and the only way to catch a
regression is to assert the flag is on every invocation - the app itself
behaves identically either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ktnmgr.enclosure.ses import BASE_ARGS, SesError, SesRunner


class _Proc:
    returncode = 0
    stdout = "output"
    stderr = ""


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    from ktnmgr.enclosure import ses as ses_module

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(ses_module.subprocess, "run", fake_run)
    return calls


def test_no_time_is_passed_on_every_page_read(
    captured: list[list[str]], tmp_path: Path
) -> None:
    runner = SesRunner(binary="/usr/bin/sg_ses", lock_path=tmp_path / "lock")
    for page in ("configuration", "enclosure_status", "additional_element_status", "join"):
        runner.read_page("/dev/sg16", page)

    assert captured, "no sg_ses invocation was made"
    for argv in captured:
        assert "--no-time" in argv, f"REPORT TIMESTAMP not suppressed: {argv}"
        # Immediately after the binary, so it cannot be swallowed by a page
        # argument that takes a value.
        assert argv[1:1 + len(BASE_ARGS)] == list(BASE_ARGS)


def test_no_time_is_passed_on_ident(captured: list[list[str]], tmp_path: Path) -> None:
    runner = SesRunner(binary="/usr/bin/sg_ses", lock_path=tmp_path / "lock")
    runner.set_ident("/dev/sg16", 0, 4, True)

    assert len(captured) == 1
    argv = captured[0]
    assert "--no-time" in argv
    assert "--index=0,4" in argv
    assert "--set=ident" in argv


def test_page_allow_list_still_rejects_unknown_pages(tmp_path: Path) -> None:
    runner = SesRunner(binary="/usr/bin/sg_ses", lock_path=tmp_path / "lock")
    with pytest.raises(SesError):
        runner.read_page("/dev/sg16", "control")
