"""Security tests (spec §43).

The hostile inputs named in the spec are tested at both layers that could
plausibly pass them through: the semantic validator that guards the privilege
boundary, and the HTTP surface itself.

The claim being tested is not "these strings are escaped" but "these strings
cannot be represented", which is a stronger property: an enclosure id must
match a hex pattern and a slot must be a bounded integer, so there is no
encoding of `7;rm -rf /` that reaches a filesystem path or an argv element.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from ktnmgr.enclosure.locate import (
    DirectLocateWriter,
    LocateError,
    validate_request,
)
from ktnmgr.enclosure.ses import READ_ONLY_PAGES, SesError, SesRunner
from ktnmgr.enclosure.sysfs import SysfsEnclosureBackend

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
LOGICAL_ID = "0x50060480aabbcc00"

# Verbatim from spec §43, plus traversal and injection variants.
HOSTILE_SLOTS = [
    "7;rm -rf /",
    "../../etc/passwd",
    "$(id)",
    "7 --set=device_off",
    "0,0 --clear=fault",
    "/dev/sg0",
    "7\n--set=ident",
    "-1",
    "7; sg_ses --set=device_off /dev/sg16",
    "%2e%2e%2f",
    "0x07",
    None,
    True,
    1.5,
    [7],
    {"slot": 7},
]

HOSTILE_ENCLOSURE_IDS = [
    "../../etc/passwd",
    "/dev/sg0",
    "0x50060480aabbcc00; rm -rf /",
    "$(id)",
    "0x50060480aabbcc00/../../..",
    "",
    "not-hex",
    "0xZZZZ",
    "0x" + "a" * 64,
    None,
    42,
]


# --------------------------------------------------------- semantic validator


@pytest.mark.parametrize("slot", HOSTILE_SLOTS)
def test_hostile_slot_is_rejected(slot: object) -> None:
    with pytest.raises(LocateError):
        validate_request(LOGICAL_ID, slot)  # type: ignore[arg-type]


@pytest.mark.parametrize("enclosure_id", HOSTILE_ENCLOSURE_IDS)
def test_hostile_enclosure_id_is_rejected(enclosure_id: object) -> None:
    with pytest.raises(LocateError):
        validate_request(enclosure_id, 0)  # type: ignore[arg-type]


def test_valid_request_is_normalised() -> None:
    assert validate_request("0X50060480AABBCC00", 7) == (LOGICAL_ID, 7)


def test_slot_upper_bound_enforced() -> None:
    with pytest.raises(LocateError):
        validate_request(LOGICAL_ID, 10_000)


def test_bool_is_not_accepted_as_slot() -> None:
    """bool is a subclass of int in Python; True must not become slot 1."""
    with pytest.raises(LocateError):
        validate_request(LOGICAL_ID, True)  # type: ignore[arg-type]


# ------------------------------------------------------------- write boundary


@pytest.fixture
def writer(tmp_path: Path) -> DirectLocateWriter:
    root = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, root)
    return DirectLocateWriter(SysfsEnclosureBackend(sysfs_root=root, dev_root=tmp_path / "dev"))


@pytest.mark.parametrize("slot", HOSTILE_SLOTS)
def test_hostile_slot_never_reaches_a_write(writer: DirectLocateWriter, slot: object) -> None:
    with pytest.raises(LocateError):
        writer.write(LOGICAL_ID, slot, True)  # type: ignore[arg-type]


def test_traversal_cannot_escape_the_enclosure_tree(writer: DirectLocateWriter, tmp_path: Path) -> None:
    """A file outside the enclosure directory must remain untouched."""
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    with pytest.raises(LocateError):
        writer.write(f"../../../{victim}", 0, True)
    assert victim.read_text() == "untouched"


def test_only_locate_is_writable(writer: DirectLocateWriter) -> None:
    """The write path targets the 'locate' attribute and nothing else (§15)."""
    ref = writer.backend.resolve(LOGICAL_ID)
    slot_dir = writer.backend.slot_dir(ref, 0)
    before = {p.name: p.read_text() for p in slot_dir.iterdir() if p.is_file()}

    writer.write(LOGICAL_ID, 0, True)

    after = {p.name: p.read_text() for p in slot_dir.iterdir() if p.is_file()}
    changed = {k for k in before if before[k] != after.get(k)}
    assert changed == {"locate"}, f"unexpected attributes written: {changed - {'locate'}}"


# ---------------------------------------------------------------- sg_ses gate


def test_ses_runner_rejects_unknown_page() -> None:
    with pytest.raises(SesError):
        SesRunner().read_page("/dev/sg16", "--set=device_off")


@pytest.mark.parametrize(
    "page",
    ["control", "--set=ident", "es; rm -rf /", "../../etc/passwd", "download_microcode"],
)
def test_ses_runner_allowlist_is_closed(page: str) -> None:
    assert page not in READ_ONLY_PAGES
    with pytest.raises(SesError):
        SesRunner().read_page("/dev/sg16", page)


def test_ses_runner_rejects_relative_device() -> None:
    with pytest.raises(SesError):
        SesRunner().read_page("sg16", "join")


def test_no_mutating_sg_ses_option_is_reachable() -> None:
    """No allow-listed page may contain a mutating sg_ses option."""
    forbidden = ("--set", "--clear", "--control", "--download", "--mode")
    for page, argv in READ_ONLY_PAGES.items():
        joined = " ".join(argv)
        for option in forbidden:
            assert option not in joined, f"page {page!r} can mutate via {option}"


# ------------------------------------------------- the one mutating SES call


def test_ident_args_contain_only_identify() -> None:
    """IDENT is the only mutating SES operation. Its arguments must address the
    identify bit and nothing else - not device_off, fault, or a PHY reset."""
    from ktnmgr.enclosure.ses import IDENT_ARGS

    assert set(IDENT_ARGS.values()) == {"--set=ident", "--clear=ident"}
    for value in IDENT_ARGS.values():
        assert "device_off" not in value
        assert "fault" not in value


@pytest.mark.parametrize("bad", [-1, 1024, 99999, True, 1.5, "0", None, "0;rm -rf /", [0]])
def test_ident_rejects_hostile_indices(bad: object) -> None:
    """Both indices are range-checked integers, so nothing else is expressible."""
    runner = SesRunner()
    with pytest.raises(SesError):
        runner.set_ident("/dev/sg16", bad, 0, True)  # type: ignore[arg-type]
    with pytest.raises(SesError):
        runner.set_ident("/dev/sg16", 0, bad, True)  # type: ignore[arg-type]


def test_ident_rejects_relative_device() -> None:
    with pytest.raises(SesError):
        SesRunner().set_ident("sg16", 0, 0, True)


def test_ident_argv_is_exactly_the_expected_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capture the argv without executing it: every element is either a fixed
    literal or a formatted integer, so no caller input reaches the shell."""
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        return Result()

    monkeypatch.setattr("ktnmgr.enclosure.ses.subprocess.run", fake_run)

    runner = SesRunner(binary="/usr/bin/sg_ses")
    runner.set_ident("/dev/sg16", 0, 7, True)
    # --no-time suppresses sg_ses's REPORT TIMESTAMP probe, which this
    # enclosure rejects; see BASE_ARGS in ses.py.
    assert captured["argv"] == [
        "/usr/bin/sg_ses", "--no-time", "--index=0,7", "--set=ident", "/dev/sg16",
    ]
    assert captured["shell"] is False

    runner.set_ident("/dev/sg16", 0, 7, False)
    assert captured["argv"][3] == "--clear=ident"


def test_ident_failure_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "sg_ses failed: Operation not permitted"

    monkeypatch.setattr("ktnmgr.enclosure.ses.subprocess.run",
                        lambda argv, **kw: Result())
    with pytest.raises(SesError, match="ident failed"):
        SesRunner().set_ident("/dev/sg16", 0, 0, True)


# ------------------------------------------------------- writer selection


def test_auto_prefers_ses_when_available(tmp_path: Path) -> None:
    """'auto' must pick the SES command path when sg_ses exists, because that
    is the one that works under the container's default AppArmor profile."""
    from ktnmgr.enclosure.locate import SesLocateWriter, build_local_locate_writer

    class PresentSes(SesRunner):
        def available(self) -> bool:
            return True

    writer = build_local_locate_writer(
        SysfsEnclosureBackend(sysfs_root=tmp_path), PresentSes(), "auto"
    )
    assert isinstance(writer, SesLocateWriter)


def test_auto_falls_back_to_sysfs_without_sg_ses(tmp_path: Path) -> None:
    from ktnmgr.enclosure.locate import DirectLocateWriter, build_local_locate_writer

    class AbsentSes(SesRunner):
        def available(self) -> bool:
            return False

    writer = build_local_locate_writer(
        SysfsEnclosureBackend(sysfs_root=tmp_path), AbsentSes(), "auto"
    )
    assert isinstance(writer, DirectLocateWriter)


def test_explicit_ses_refuses_to_silently_downgrade(tmp_path: Path) -> None:
    """Asking for 'ses' and getting sysfs would quietly reintroduce the need for
    a writable /sys, so it must error instead."""
    from ktnmgr.enclosure.locate import build_local_locate_writer

    class AbsentSes(SesRunner):
        def available(self) -> bool:
            return False

    with pytest.raises(LocateError):
        build_local_locate_writer(
            SysfsEnclosureBackend(sysfs_root=tmp_path), AbsentSes(), "ses"
        )


def test_unknown_method_is_rejected(tmp_path: Path) -> None:
    from ktnmgr.enclosure.locate import build_local_locate_writer

    with pytest.raises(LocateError):
        build_local_locate_writer(
            SysfsEnclosureBackend(sysfs_root=tmp_path), SesRunner(), "whatever"
        )
