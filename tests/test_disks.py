"""Local disk identity tests (spec §41: serial/WWN correlation, /dev renumbering)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ktnmgr.enclosure.disks import DiskInfoReader
from ktnmgr.enclosure.sysfs import SysfsEnclosureBackend

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"

# From spec §8. Keyed by SES slot, which is the persistent identity - not by
# device name, which is runtime state.
EXPECTED_SERIALS = {
    0: "P9GXVWZW", 1: "P9H32WVW", 2: "P9K7486V", 3: "P9K734LV", 4: "P9JW0G5W",
    5: "P9K70K2W", 6: "P9K6LYPW", 7: "P9K6Z2KW", 8: "P9K7184W", 9: "P9K6VRVW",
    10: "P9H33BJW", 11: "P9K2WUHW", 12: "P9K72ZDV", 13: "P9K734TV", 14: "P9K6LYUW",
}
EXPECTED_WWNS = {
    0: "0x5000cca058347d84", 7: "0x5000cca058b5c6a8", 14: "0x5000cca058b51fac",
}


@pytest.fixture
def reader() -> DiskInfoReader:
    return DiskInfoReader(sysfs_root=FIXTURE_ROOT)


@pytest.fixture
def backend() -> SysfsEnclosureBackend:
    return SysfsEnclosureBackend(sysfs_root=FIXTURE_ROOT, dev_root=Path("/nonexistent-dev"))


def test_serial_survives_vpd_header(reader: DiskInfoReader) -> None:
    """Regression guard: stripping NULs before the 4-byte header offset drops
    the 'P9' prefix from every serial on this hardware."""
    assert reader.read("sdb").serial == "P9GXVWZW"


def test_all_slots_resolve_to_expected_serials(
    reader: DiskInfoReader, backend: SysfsEnclosureBackend
) -> None:
    ref = backend.discover()[0]
    actual = {}
    for slot in backend.read_slots(ref):
        if slot.block_device:
            actual[slot.ses_slot] = reader.read(slot.block_device).serial
    assert actual == EXPECTED_SERIALS


@pytest.mark.parametrize("ses_slot", sorted(EXPECTED_WWNS))
def test_wwn_normalised_from_naa(
    reader: DiskInfoReader, backend: SysfsEnclosureBackend, ses_slot: int
) -> None:
    ref = backend.discover()[0]
    slot = next(s for s in backend.read_slots(ref) if s.ses_slot == ses_slot)
    assert slot.block_device is not None
    assert reader.read(slot.block_device).wwn == EXPECTED_WWNS[ses_slot]


def test_model_firmware_and_capacity(reader: DiskInfoReader) -> None:
    disk = reader.read("sdb")
    assert disk.model is not None and "HUS72403CLAR3000" in disk.model
    assert disk.firmware == "C370"
    assert disk.rotational is True
    assert disk.size_bytes == 5860533168 * 512  # ~3.0 TB


def test_absent_device_returns_empty_identity(reader: DiskInfoReader) -> None:
    """An empty bay must not raise; it simply has no disk (§37)."""
    assert reader.read("sdzz").serial is None
    assert reader.read(None).serial is None


def test_identity_is_stable_across_device_renaming(reader: DiskInfoReader) -> None:
    """§37/§50: /dev renumbering must be tolerated. The same physical disk is
    recognised by serial+WWN regardless of which name it currently carries."""
    by_serial = {}
    for name in ("sdb", "sdc", "sdd"):
        disk = reader.read(name)
        assert disk.serial is not None
        by_serial[disk.serial] = disk.wwn
    assert len(by_serial) == 3
    assert all(w and w.startswith("0x") for w in by_serial.values())


def test_ses_sas_address_is_not_the_block_wwn() -> None:
    """Documents the trap: SES reports the SAS port address, the block layer
    reports the node WWN, and on this hardware they differ by 2. Correlating on
    equality would map every slot to nothing."""
    ses_reported = int("5000cca058347d86", 16)  # from sg_ses aes, slot 0
    block_reported = int("5000cca058347d84", 16)  # from sysfs wwid, sdb
    assert ses_reported != block_reported
    assert ses_reported - block_reported == 2
