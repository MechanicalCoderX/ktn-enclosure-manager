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
LOGICAL_ID = "0x5006048004a54c3e"

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
    "0x5006048004a54c3e; rm -rf /",
    "$(id)",
    "0x5006048004a54c3e/../../..",
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
    assert validate_request("0X5006048004A54C3E", 7) == (LOGICAL_ID, 7)


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
