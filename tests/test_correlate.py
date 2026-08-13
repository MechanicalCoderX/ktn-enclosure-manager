"""TrueNAS correlation tests against captured payloads (spec §41)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ktnmgr.enclosure.disks import DiskInfoReader
from ktnmgr.enclosure.sysfs import SysfsEnclosureBackend
from ktnmgr.models import DiskIdentity, ZfsState
from ktnmgr.truenas.correlate import (
    build_disk_index,
    build_smart_index,
    build_zfs_index,
    merge_identity,
)

TN = Path(__file__).parent / "fixtures" / "truenas"
SYSFS = Path(__file__).parent / "fixtures" / "sysfs_root"

SHELF_DISKS = {
    "sdb", "sdc", "sdd", "sde", "sdf", "sdg", "sdh", "sdi",
    "sdj", "sdk", "sdl", "sdm", "sdn", "sdo", "sdp",
}


@pytest.fixture(scope="module")
def pools() -> list[dict]:
    return json.loads((TN / "pool_query.json").read_text())


@pytest.fixture(scope="module")
def disks() -> list[dict]:
    return json.loads((TN / "disk_query.json").read_text())


@pytest.fixture(scope="module")
def temperatures() -> dict:
    return json.loads((TN / "disk_temperatures.json").read_text())


# ------------------------------------------------------------------- topology


def test_all_shelf_disks_attributed_to_pool(pools: list[dict]) -> None:
    index = build_zfs_index(pools)
    assert SHELF_DISKS <= set(index), f"unattributed: {SHELF_DISKS - set(index)}"


def test_vdev_name_is_the_raidz_container_not_the_leaf(pools: list[dict]) -> None:
    """A disk in raidz3-0 must report 'raidz3-0', not its own device name."""
    index = build_zfs_index(pools)
    assert index["sdb"].pool == "tank"
    assert index["sdb"].vdev == "raidz3-0"


def test_zfs_state_and_error_counters(pools: list[dict]) -> None:
    index = build_zfs_index(pools)
    for name in SHELF_DISKS:
        info = index[name]
        assert info.state is ZfsState.ONLINE
        assert info.read_errors == 0
        assert info.write_errors == 0
        assert info.checksum_errors == 0
        assert info.is_spare is False


def test_no_resilver_in_baseline(pools: list[dict]) -> None:
    index = build_zfs_index(pools)
    assert all(not info.resilvering for info in index.values())


def test_empty_topology_is_not_an_error() -> None:
    assert build_zfs_index([]) == {}
    assert build_zfs_index([{"name": "p", "topology": {}}]) == {}


def test_mirror_and_spare_groups_are_walked() -> None:
    """Synthetic topology: the walker must handle nested mirrors and spares,
    neither of which appears in this shelf's single-RAIDZ3 baseline."""
    pools = [
        {
            "name": "tank",
            "scan": {"function": "RESILVER", "state": "SCANNING"},
            "topology": {
                "data": [
                    {
                        "type": "MIRROR",
                        "name": "mirror-0",
                        "children": [
                            {"type": "DISK", "disk": "sdx", "status": "ONLINE", "stats": {}},
                            {"type": "DISK", "disk": "sdy", "status": "DEGRADED",
                             "stats": {"read_errors": 3}},
                        ],
                    }
                ],
                "spare": [{"type": "DISK", "name": "sdz", "disk": "sdz", "status": "AVAIL"}],
            },
        }
    ]
    index = build_zfs_index(pools)
    assert index["sdx"].vdev == "mirror-0"
    assert index["sdy"].state is ZfsState.DEGRADED
    assert index["sdy"].read_errors == 3
    assert index["sdz"].is_spare is True
    assert index["sdx"].resilvering is True


def test_unknown_zfs_status_degrades_gracefully() -> None:
    pools = [{"name": "p", "topology": {"data": [
        {"type": "DISK", "name": "d", "disk": "sdq", "status": "WEIRD-NEW-STATE"}]}}]
    assert build_zfs_index(pools)["sdq"].state is ZfsState.UNKNOWN


# ----------------------------------------------------------------- disk query


def test_disk_index_serials(disks: list[dict]) -> None:
    index = build_disk_index(disks)
    assert index["sdb"].serial == "K1A00001"
    assert index["sdb"].model == "HUS72403CLAR3000"
    assert index["sdb"].size_bytes == 3000592982016


def test_truenas_pool_field_is_unreliable(disks: list[dict]) -> None:
    """Documents why pool membership comes from pool.query: disk.query reports
    pool=None on 25.10.5 even for disks that are pool members."""
    shelf = [d for d in disks if d["name"] in SHELF_DISKS]
    assert shelf, "fixture should contain shelf disks"
    assert all(d.get("pool") is None for d in shelf)


def test_truenas_enclosure_field_is_empty(disks: list[dict]) -> None:
    """enclosure2 is hardware-gated off, so TrueNAS cannot supply slot mapping;
    this is precisely the gap this application fills."""
    assert all(d.get("enclosure") is None for d in disks)


# ---------------------------------------------------------------------- SMART


def test_missing_temperature_is_none_not_zero(temperatures: dict) -> None:
    index = build_smart_index(temperatures)
    unread = [name for name, info in index.items() if not info.available]
    for name in unread:
        assert index[name].temperature_c is None
    readable = [i for i in index.values() if i.available]
    assert readable, "at least one disk should report a temperature"
    assert all(0 < i.temperature_c < 100 for i in readable)


# ------------------------------------------------------------------- identity


def test_local_sysfs_wins_over_truenas(disks: list[dict]) -> None:
    local = DiskIdentity(serial="LOCAL1", firmware="C370", wwn="0xaaaa")
    remote = build_disk_index(disks)["sdb"]
    merged = merge_identity(local, remote)
    assert merged.serial == "LOCAL1"
    assert merged.firmware == "C370"
    assert merged.wwn == "0xaaaa"
    # gaps are filled from TrueNAS
    assert merged.size_bytes == 3000592982016
    assert merged.transport == "SCSI"


def test_merge_tolerates_absent_remote() -> None:
    local = DiskIdentity(serial="ONLY-LOCAL")
    assert merge_identity(local, None).serial == "ONLY-LOCAL"


def test_end_to_end_slot_to_pool(pools: list[dict], disks: list[dict]) -> None:
    """The full chain the UI depends on: SES slot -> block device -> ZFS vdev."""
    backend = SysfsEnclosureBackend(sysfs_root=SYSFS, dev_root=Path("/nonexistent"))
    reader = DiskInfoReader(sysfs_root=SYSFS)
    zfs = build_zfs_index(pools)
    remote = build_disk_index(disks)

    ref = backend.discover()[0]
    resolved = 0
    for slot in backend.read_slots(ref):
        if not slot.block_device:
            continue
        identity = merge_identity(reader.read(slot.block_device), remote.get(slot.block_device))
        info = zfs[slot.block_device]
        assert identity.serial
        assert info.pool == "tank"
        assert info.vdev == "raidz3-0"
        resolved += 1
    assert resolved == 15
