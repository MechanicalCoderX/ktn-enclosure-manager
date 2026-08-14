"""Fixture-driven tests for enclosure discovery and slot mapping (spec §41).

Every assertion here is checked against a captured KTN-STL3 tree, so the suite
runs with no hardware attached.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ktnmgr.enclosure.sysfs import (
    EnclosureNotFoundError,
    SlotNotFoundError,
    SysfsEnclosureBackend,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
LOGICAL_ID = "0x50060480aabbcc00"

# The authoritative mapping from spec §8. The tail is deliberately
# non-alphabetical (sdn, sdp, sdo, sdm); that is what proves the mapping comes
# from sysfs rather than from sorting device names.
EXPECTED_MAP = {
    0: "sdb", 1: "sdc", 2: "sdd", 3: "sde", 4: "sdf",
    5: "sdg", 6: "sdh", 7: "sdi", 8: "sdj", 9: "sdk",
    10: "sdl", 11: "sdn", 12: "sdp", 13: "sdo", 14: "sdm",
}


@pytest.fixture
def backend() -> SysfsEnclosureBackend:
    return SysfsEnclosureBackend(sysfs_root=FIXTURE_ROOT, dev_root=Path("/nonexistent-dev"))


def test_discovers_exactly_one_enclosure(backend: SysfsEnclosureBackend) -> None:
    found = backend.discover()
    assert len(found) == 1


def test_enclosure_identity_is_from_attributes_not_path(backend: SysfsEnclosureBackend) -> None:
    ref = backend.discover()[0]
    assert ref.logical_id == LOGICAL_ID
    assert ref.vendor == "EMC"
    assert ref.product == "ESES Enclosure"
    assert ref.revision == "0001"
    assert ref.scsi_address == "1:0:15:0"


def test_slot_count_excludes_non_slot_directories(backend: SysfsEnclosureBackend) -> None:
    """'device', 'power' and the decoy dir carry no 'slot' attribute."""
    ref = backend.discover()[0]
    # 15 populated bays + 1 empty bay fixture; the decoy is excluded.
    assert ref.slot_count == 16


def test_fifteen_populated_bays(backend: SysfsEnclosureBackend) -> None:
    ref = backend.discover()[0]
    slots = backend.read_slots(ref)
    populated = [s for s in slots if s.block_device]
    assert len(populated) == 15


def test_slot_to_block_device_mapping_matches_hardware(backend: SysfsEnclosureBackend) -> None:
    ref = backend.discover()[0]
    actual = {s.ses_slot: s.block_device for s in backend.read_slots(ref) if s.block_device}
    assert actual == EXPECTED_MAP


def test_non_alphabetical_tail_is_preserved(backend: SysfsEnclosureBackend) -> None:
    """Regression guard: a naive implementation that sorts /dev/sdX names would
    map 11->sdm, 12->sdn, 13->sdo, 14->sdp. The hardware says otherwise."""
    ref = backend.discover()[0]
    actual = {s.ses_slot: s.block_device for s in backend.read_slots(ref)}
    assert actual[11] == "sdn"
    assert actual[12] == "sdp"
    assert actual[13] == "sdo"
    assert actual[14] == "sdm"


@pytest.mark.parametrize(("bay", "ses"), [(1, 0), (8, 7), (15, 14)])
def test_bay_numbering_contract(backend: SysfsEnclosureBackend, bay: int, ses: int) -> None:
    """§50: GUI Bay 1 -> SES 0, Bay 8 -> SES 7, Bay 15 -> SES 14."""
    ref = backend.discover()[0]
    slot = next(s for s in backend.read_slots(ref) if s.ses_slot == ses)
    assert slot.display_bay == bay


def test_all_bays_healthy_in_baseline(backend: SysfsEnclosureBackend) -> None:
    ref = backend.discover()[0]
    for slot in backend.read_slots(ref):
        if slot.block_device is None:
            continue
        assert slot.status == "OK"
        assert slot.power_status == "on"
        assert slot.locate is False
        assert slot.fault is False


def test_empty_bay_is_reported_without_device(backend: SysfsEnclosureBackend) -> None:
    ref = backend.discover()[0]
    empty = next(s for s in backend.read_slots(ref) if s.ses_slot == 99)
    assert empty.block_device is None
    assert empty.status == "not installed"


def test_resolve_by_logical_id(backend: SysfsEnclosureBackend) -> None:
    assert backend.resolve(LOGICAL_ID).logical_id == LOGICAL_ID
    assert backend.resolve(LOGICAL_ID.upper()).logical_id == LOGICAL_ID


def test_resolve_unknown_enclosure_raises(backend: SysfsEnclosureBackend) -> None:
    with pytest.raises(EnclosureNotFoundError):
        backend.resolve("0xdeadbeefdeadbeef")


def test_unknown_slot_raises(backend: SysfsEnclosureBackend) -> None:
    ref = backend.discover()[0]
    with pytest.raises(SlotNotFoundError):
        backend.slot_dir(ref, 42)


def test_locate_write_and_readback(tmp_path: Path) -> None:
    """set_locate must verify by reading the value back, not assume the write took."""
    import shutil

    root = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, root)
    backend = SysfsEnclosureBackend(sysfs_root=root, dev_root=tmp_path / "dev")
    ref = backend.resolve(LOGICAL_ID)

    assert backend.read_locate(ref, 0) is False
    assert backend.set_locate(ref, 0, True) is True
    assert backend.read_locate(ref, 0) is True
    assert backend.set_locate(ref, 0, False) is False
    assert backend.read_locate(ref, 0) is False


def test_missing_sysfs_root_returns_empty_not_crash(tmp_path: Path) -> None:
    """§37: enclosure disconnected / kernel support absent must degrade, not raise."""
    backend = SysfsEnclosureBackend(sysfs_root=tmp_path / "nope", dev_root=tmp_path)
    assert backend.discover() == []


class _SlowSettlingBackend(SysfsEnclosureBackend):
    """Simulates the real hardware: the attribute keeps reporting the old value
    for the first few reads after a write."""

    stale_reads = 3

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._remaining = 0

    def set_locate(self, ref, ses_slot, on, **kwargs):  # type: ignore[no-untyped-def]
        self._remaining = self.stale_reads
        return super().set_locate(ref, ses_slot, on, **kwargs)

    def read_locate_at(self, path: Path) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return not super().read_locate_at(path)  # stale: the previous value
        return super().read_locate_at(path)


def test_locate_readback_polls_until_the_value_settles(tmp_path: Path) -> None:
    """Regression guard for a bug only real hardware exposed: sysfs does not
    update synchronously with the write, so a single immediate read returns the
    previous value and every IDENT would be reported as failed verification."""
    import shutil

    root = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, root)
    backend = _SlowSettlingBackend(sysfs_root=root, dev_root=tmp_path / "dev")
    ref = backend.resolve(LOGICAL_ID)

    assert backend.set_locate(ref, 0, True, poll_interval=0.001) is True
    assert backend.set_locate(ref, 0, False, poll_interval=0.001) is False


def test_locate_readback_gives_up_and_reports_failure(tmp_path: Path) -> None:
    """If it never settles, the caller must learn that - not be told it worked."""
    import shutil

    root = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, root)
    backend = _SlowSettlingBackend(sysfs_root=root, dev_root=tmp_path / "dev")
    backend.stale_reads = 10_000
    ref = backend.resolve(LOGICAL_ID)

    assert backend.set_locate(ref, 0, True, settle_timeout=0.05, poll_interval=0.001) is False
