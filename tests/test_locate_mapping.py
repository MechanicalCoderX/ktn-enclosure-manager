"""Slot-number -> element-index translation in the SES IDENT path.

The slot a caller passes to SesLocateWriter.write() originates from the sysfs
``slot`` attribute, which the kernel ses driver fills from the additional
element status page's vendor-assigned *device slot number*. ``sg_ses
--index=T,E`` addresses the 0-based *element index* within type descriptor T.
SES-3 gives no guarantee the two coincide: on the KTN-STL3 they happen to be
equal, but other shelves number slots 1-based or permuted, and passing the
slot straight through as an element index lights the WRONG bay's "pull this
disk" LED there. These tests prove the writer translates through the
enclosure's own AES-page mapping - and refuses when there is no unambiguous
mapping - instead of assuming identity.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from ktnmgr.enclosure.locate import LocateError, SesLocateWriter
from ktnmgr.enclosure.ses import SesError, SesResult

FIXTURES = Path(__file__).parent / "fixtures" / "ktn-stl3"
SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"

STL3_ID = "0x50060480aabbcc00"
PERMUTED_ID = "0x5000000000000002"

#: A shelf whose firmware (wrongly) assigns the same device slot number to two
#: elements. No sg_ses capture of such a shelf exists, so the shape is
#: hand-written; the parser-level format is validated against the real capture
#: in test_ses_parser.py.
DUPLICATE_SLOT_AES = """\
  ACME      SES Shelf         0001
  Primary enclosure logical identifier (hex): 5000000000000002
Additional element status diagnostic page:
  generation code: 0x1
  additional element status descriptor list
    Element type: Device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=1
        Transport protocol: SAS
        number of phys: 1, not all phys: 1, device slot number: 2
      Element index: 1  eiioe=1
        Transport protocol: SAS
        number of phys: 1, not all phys: 1, device slot number: 2
"""


class FakeSes:
    """Serves captured page text and simulates the enclosure honouring IDENT.

    ``wiring`` maps element_index -> the slot directory whose locate attribute
    the (simulated) enclosure processor updates for that element. Applying the
    IDENT through the wiring instead of echoing the caller's arguments back is
    the point of this fake: a writer that passed the sysfs slot straight
    through as an element index would flip the WRONG bay's locate file on the
    permuted shelf, and the settle verification would observe it.
    """

    def __init__(self, pages: dict[str, str], wiring: dict[int, Path]) -> None:
        self.pages = pages
        self.wiring = wiring
        self.ident_calls: list[tuple[str, int, int, bool]] = []
        self.page_reads: list[str] = []

    def read_page(self, device: str, page: str) -> SesResult:
        self.page_reads.append(page)
        if page not in self.pages:
            raise SesError(f"no fixture for page {page!r}")
        return SesResult(page=page, stdout=self.pages[page], returncode=0)

    def set_ident(self, device: str, type_index: int, element_index: int, on: bool) -> None:
        self.ident_calls.append((device, type_index, element_index, on))
        target = self.wiring.get(element_index)
        if target is not None:
            (target / "locate").write_text("1" if on else "0")


class FakeBackend:
    """Just enough of SysfsEnclosureBackend for SesLocateWriter.write()."""

    lock_path = None

    def __init__(self, root: Path, ref: SimpleNamespace, slots: list[int]) -> None:
        self.ref = ref
        self.dirs: dict[int, Path] = {}
        for slot in slots:
            slot_dir = root / str(slot)
            slot_dir.mkdir()
            (slot_dir / "locate").write_text("0")
            self.dirs[slot] = slot_dir

    def resolve(self, enclosure_id: str) -> SimpleNamespace:
        return self.ref

    def slot_dir(self, ref: SimpleNamespace, slot: int) -> Path:
        return self.dirs[slot]

    def read_locate_at(self, path: Path) -> bool:
        return path.read_text().strip() not in ("0", "", "off")


def _writer(
    tmp_path: Path,
    logical_id: str,
    pages: dict[str, str],
    slots: list[int],
    wiring: dict[int, int],
) -> tuple[SesLocateWriter, FakeSes]:
    """``wiring`` is element_index -> sysfs slot number, i.e. the physical
    backplane routing the AES page describes."""
    ref = SimpleNamespace(logical_id=logical_id, sg_device="/dev/sg16")
    backend = FakeBackend(tmp_path, ref, slots)
    ses = FakeSes(pages, {element: backend.dirs[slot] for element, slot in wiring.items()})
    return SesLocateWriter(backend, ses), ses  # type: ignore[arg-type]


@pytest.fixture
def stl3(tmp_path: Path) -> tuple[SesLocateWriter, FakeSes]:
    pages = {
        "configuration": (FIXTURES / "sg_cf.txt").read_text(),
        "additional_element_status": (FIXTURES / "sg_aes.txt").read_text(),
    }
    # On this shelf the AES page states element N occupies device slot N.
    return _writer(tmp_path, STL3_ID, pages, list(range(15)), {n: n for n in range(15)})


@pytest.fixture
def permuted(tmp_path: Path) -> tuple[SesLocateWriter, FakeSes]:
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        "additional_element_status": (SYNTHETIC / "sg_aes_permuted.txt").read_text(),
    }
    # Per the fixture's AES page: element 0 sits in bay 4, element 1 in bay 1,
    # element 3 in bay 2; element 2 is an empty bay with no slot number.
    return _writer(tmp_path, PERMUTED_ID, pages, [1, 2, 3, 4], {0: 4, 1: 1, 3: 2})


# ------------------------------------------------------------------ KTN-STL3


def test_stl3_map_is_identity_from_real_capture(stl3: tuple[SesLocateWriter, FakeSes]) -> None:
    """On the real KTN-STL3 capture the translation must be the identity for
    all 15 bays - anything else would break the shelf this app runs on."""
    writer, ses = stl3
    for slot in range(15):
        assert writer.write(STL3_ID, slot, True) is True
    assert ses.ident_calls == [("/dev/sg16", 0, slot, True) for slot in range(15)]
    assert writer._slot_to_element[STL3_ID] == {n: n for n in range(15)}


def test_stl3_pages_are_read_once_per_enclosure(stl3: tuple[SesLocateWriter, FakeSes]) -> None:
    """Both the configuration and AES reads are per-enclosure topology, so
    repeated writes must not re-read them (each read is a SCSI round trip
    holding the enclosure lock)."""
    writer, ses = stl3
    writer.write(STL3_ID, 3, True)
    writer.write(STL3_ID, 7, True)
    writer.write(STL3_ID, 3, False)
    assert ses.page_reads.count("configuration") == 1
    assert ses.page_reads.count("additional_element_status") == 1


# ------------------------------------------------------------ permuted shelf


def test_permuted_shelf_translates_slot_to_element(
    permuted: tuple[SesLocateWriter, FakeSes],
) -> None:
    """The wrong-bay regression test: the element index that reaches set_ident
    must come from the AES mapping, not from the sysfs slot number."""
    writer, ses = permuted
    assert writer.write(PERMUTED_ID, 4, True) is True
    assert ses.ident_calls[-1] == ("/dev/sg16", 0, 0, True), "bay 4 is element 0, not element 4"
    assert writer.write(PERMUTED_ID, 2, True) is True
    assert ses.ident_calls[-1] == ("/dev/sg16", 0, 3, True), "bay 2 is element 3, not element 2"
    assert writer.write(PERMUTED_ID, 1, False) is False
    assert ses.ident_calls[-1] == ("/dev/sg16", 0, 1, False)


def test_permuted_shelf_lights_the_requested_bay_only(
    permuted: tuple[SesLocateWriter, FakeSes],
) -> None:
    """End-to-end through the simulated backplane wiring: after asking for bay
    4, bay 4's locate attribute is lit and no other bay's is - the exact
    property the original code violated on permuted shelves."""
    writer, _ = permuted
    assert writer.write(PERMUTED_ID, 4, True) is True
    backend = writer.backend
    lit = {slot for slot, d in backend.dirs.items() if backend.read_locate_at(d / "locate")}
    assert lit == {4}


def test_unmapped_slot_is_refused_not_guessed(permuted: tuple[SesLocateWriter, FakeSes]) -> None:
    """Slot 3 exists in sysfs but the AES page offers no element for it (the
    fixture's empty bay carries no device slot number). Guessing would light
    some other bay, so the writer must refuse - and say what it looked for
    and what the enclosure offered."""
    writer, ses = permuted
    with pytest.raises(LocateError, match=r"slot 3.*offers device slot numbers \[1, 2, 4\]"):
        writer.write(PERMUTED_ID, 3, True)
    assert ses.ident_calls == [], "a refused IDENT must not reach the enclosure"


def test_duplicate_slot_numbers_are_refused(tmp_path: Path) -> None:
    """Two elements claiming the same device slot number cannot be addressed
    unambiguously; the writer must refuse rather than pick one."""
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        "additional_element_status": DUPLICATE_SLOT_AES,
    }
    writer, ses = _writer(tmp_path, PERMUTED_ID, pages, [2], {})
    with pytest.raises(LocateError, match="cannot address device slot number 2"):
        writer.write(PERMUTED_ID, 2, True)
    assert ses.ident_calls == []


# ------------------------------------------------------------- failure modes


def test_aes_read_failure_becomes_locate_error(tmp_path: Path) -> None:
    """A failed AES read must surface as LocateError (the writer's contract),
    never as a raw SesError or a fallthrough to the untranslated slot."""
    pages = {"configuration": (FIXTURES / "sg_cf.txt").read_text()}
    writer, ses = _writer(tmp_path, STL3_ID, pages, [0], {})
    with pytest.raises(LocateError, match="additional element status"):
        writer.write(STL3_ID, 0, True)
    assert ses.ident_calls == []


# ---------------------------------------------------- coordinate systems
#
# The four fixtures below are the regression suite for the global-vs-per-type
# index bug: sg_ses prints the AES descriptor's raw ELEMENT INDEX field,
# which is GLOBAL across type descriptors (the real KTN capture proves it),
# while --index=T,E consumes the 0-based index WITHIN type T. Feeding the
# printed value to --index is only ever correct on a shelf whose bay
# descriptor happens to come first with eiioe=0 - such as the KTN-STL3,
# which is exactly why the bug survived N=1 hardware.

BAYS_SECOND_ID = "0x5000000000000003"
EIIOE1_ID = "0x5000000000000004"
EIP0_ID = "0x5000000000000005"
CROSS_BLOCK_ID = "0x5000000000000006"


def test_bays_second_shelf_uses_per_type_position(tmp_path: Path) -> None:
    """The bay block sits AFTER a one-element expander block, so its printed
    indexes run 1..4 while its per-type positions run 0..3. Requesting bay 1
    must issue --index=1,0; the printed value 1 would light bay 2."""
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_bays_second.txt").read_text(),
        "additional_element_status": (SYNTHETIC / "sg_aes_bays_second.txt").read_text(),
    }
    writer, ses = _writer(
        tmp_path, BAYS_SECOND_ID, pages, [1, 2, 3, 4], {0: 1, 1: 2, 2: 3, 3: 4}
    )
    assert writer.write(BAYS_SECOND_ID, 1, True) is True
    assert ses.ident_calls[-1] == ("/dev/sg16", 1, 0, True)
    backend = writer.backend
    lit = {slot for slot, d in backend.dirs.items() if backend.read_locate_at(d / "locate")}
    assert lit == {1}, "the printed global index would have lit bay 2"
    assert writer.write(BAYS_SECOND_ID, 4, True) is True
    assert ses.ident_calls[-1] == ("/dev/sg16", 1, 3, True)


def test_eiioe1_shelf_ignores_the_shifted_printed_index(tmp_path: Path) -> None:
    """Under eiioe=1 the printed index additionally counts overall elements,
    so the first bay prints 1. Position stays the truth: bay 0 is element 0."""
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        "additional_element_status": (SYNTHETIC / "sg_aes_eiioe1.txt").read_text(),
    }
    writer, ses = _writer(tmp_path, EIIOE1_ID, pages, [0, 1, 2, 3], {n: n for n in range(4)})
    assert writer.write(EIIOE1_ID, 0, True) is True
    assert ses.ident_calls[-1] == ("/dev/sg16", 0, 0, True)


def test_eip0_descriptor_is_addressable_by_position(tmp_path: Path) -> None:
    """An eip=0 descriptor ('Element N descriptor', no index field) still
    occupies a position; the bay it claims must be addressable."""
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        "additional_element_status": (SYNTHETIC / "sg_aes_eip0.txt").read_text(),
    }
    writer, ses = _writer(tmp_path, EIP0_ID, pages, [10, 11, 12], {0: 10, 1: 11, 2: 12})
    assert writer.write(EIP0_ID, 11, True) is True
    assert ses.ident_calls[-1] == ("/dev/sg16", 0, 1, True)


def test_inconsistent_element_numbering_poisons_the_block(tmp_path: Path) -> None:
    """A block whose printed indexes repeat (real firmware does this; sg_ses
    carries a workaround for bogus repeated ei=0) fails the ordering
    cross-check: if the firmware cannot number its elements, its descriptor
    order cannot be trusted either, so every bay it claims is refused."""
    broken = DUPLICATE_SLOT_AES.replace("device slot number: 2", "device slot number: 5", 1)
    broken = broken.replace("Element index: 1", "Element index: 0")
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        "additional_element_status": broken,
    }
    writer, ses = _writer(tmp_path, PERMUTED_ID, pages, [2, 5], {})
    with pytest.raises(LocateError, match="cannot address device slot number 5"):
        writer.write(PERMUTED_ID, 5, True)
    assert ses.ident_calls == []


def test_cross_block_slot_claims_are_ambiguous(tmp_path: Path) -> None:
    """The kernel builds sysfs slots from every bay-type block, so a slot
    number claimed by the chosen block AND a foreign bay block cannot say
    which bay it means - refused. A slot claimed only by the foreign block is
    simply unmapped for this type descriptor."""
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_cross_block.txt").read_text(),
        "additional_element_status": (SYNTHETIC / "sg_aes_cross_block.txt").read_text(),
    }
    writer, ses = _writer(tmp_path, CROSS_BLOCK_ID, pages, [1, 2, 9], {})
    with pytest.raises(LocateError, match="cannot address device slot number 2"):
        writer.write(CROSS_BLOCK_ID, 2, True)
    with pytest.raises(LocateError, match=r"do not include 9"):
        writer.write(CROSS_BLOCK_ID, 9, True)
    assert ses.ident_calls == []


def test_empty_slot_map_is_not_cached(tmp_path: Path) -> None:
    """An AES read that succeeds but maps nothing must not be cached: a
    one-off anomalous read would otherwise pin every future IDENT to refusal
    until restart. The page is re-read on the next attempt instead."""
    empty_aes = "\n".join(DUPLICATE_SLOT_AES.splitlines()[:5]) + "\n"
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        "additional_element_status": empty_aes,
    }
    writer, ses = _writer(tmp_path, PERMUTED_ID, pages, [0], {})
    with pytest.raises(LocateError, match="maps no device slot numbers"):
        writer.write(PERMUTED_ID, 0, True)
    with pytest.raises(LocateError, match="maps no device slot numbers"):
        writer.write(PERMUTED_ID, 0, True)
    assert ses.page_reads.count("additional_element_status") == 2
    assert ses.ident_calls == []
