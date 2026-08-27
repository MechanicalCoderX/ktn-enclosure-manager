"""State service regressions from the v1.5.4 pre-submission review.

Four verified findings, all in services/state.py:

1. classify() tested has_device before the SES fault bit, so a drive that
   failed hard enough for the kernel to delete its SCSI device - while the
   enclosure still asserted the slot's fault indicator - rendered as an EMPTY
   bay on the shelf map.
2. poll_hardware() ran discover()/read_slots() inline on the event loop; both
   take the cross-process enclosure lock (busy-wait up to 30s), so a wedged
   shelf stalled the whole HTTP surface, healthz included.
3. _sas_addresses() keyed the AES join on element index while bays() looks up
   by the sysfs slot number - the same numbering conflation the IDENT path
   had to fix (test_locate_mapping.py), display-only.
4. The TrueNAS disk/ZFS caches are keyed by transient block name, so a
   same-name drive swap wore the removed drive's identity and ZFS state for
   up to one poll_truenas interval.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ktnmgr.enclosure.ses import SesError, SesResult
from ktnmgr.models import (
    DiskIdentity,
    EnclosureRef,
    SlotHealth,
    SlotState,
    ZfsInfo,
    ZfsState,
)
from ktnmgr.services.state import (
    StateService,
    _identity_conflict,
    _slot_sas_addresses,
    classify,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ktn-stl3"
SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"

STL3_ID = "0x50060480aabbcc00"
PERMUTED_ID = "0x5000000000000002"


def _ref(logical_id: str = STL3_ID) -> EnclosureRef:
    return EnclosureRef(
        logical_id=logical_id,
        vendor="EMC",
        product="ESES Enclosure",
        revision="0001",
        scsi_address="1:0:15:0",
        sysfs_path="/sys/class/enclosure/1:0:15:0",
        sg_device="/dev/sg16",
        slot_count=15,
    )


def _slot(ses_slot: int, block_device: str | None = None) -> SlotState:
    return SlotState(
        ses_slot=ses_slot,
        display_bay=ses_slot + 1,
        status="OK",
        block_device=block_device,
        sysfs_path=f"/sys/class/enclosure/1:0:15:0/{ses_slot}",
    )


class FakeSettings:
    def allowed_enclosures(self) -> set[str]:
        return set()


class FakeDiskReader:
    def __init__(self, identities: dict[str, DiskIdentity] | None = None) -> None:
        self.identities = identities or {}

    def read(self, name: str | None) -> DiskIdentity:
        if not name:
            return DiskIdentity()
        return self.identities.get(name, DiskIdentity())


class FakeIdent:
    def describe(self, enclosure_id: str, ses_slot: int, locate: bool) -> tuple[None, None]:
        return (None, None)


class FakeSes:
    """Serves captured page text; pages in ``fail`` raise like sg_ses would."""

    def __init__(self, pages: dict[str, str], fail: set[str] | None = None) -> None:
        self.pages = pages
        self.fail = fail or set()

    def available(self) -> bool:
        return True

    def read_for(self, ref: Any, page: str) -> SesResult:
        if page in self.fail:
            raise SesError(f"simulated failure reading {page}")
        return SesResult(page=page, stdout=self.pages[page], returncode=0)


def _service(
    ses: Any = None,
    backend: Any = None,
    disks: FakeDiskReader | None = None,
    settings: Any = None,
) -> StateService:
    return StateService(
        settings=settings,
        backend=backend,
        disks=disks or FakeDiskReader(),
        ses=ses,
        ident=FakeIdent(),
        truenas=None,
    )


# ------------------------------------------------- finding 1: fault vs. empty


def test_faulted_deviceless_bay_is_failed_not_empty() -> None:
    """A drive can fail hard enough that the kernel deletes its SCSI device
    while the enclosure still asserts the slot fault indicator. That bay is
    the one most in need of attention; it must never render as EMPTY."""
    assert classify("OK", True, False, ZfsInfo()) is SlotHealth.FAILED


def test_critical_status_outranks_missing_device() -> None:
    assert classify("Critical", False, False, ZfsInfo()) is SlotHealth.FAILED
    assert classify("Unrecoverable", False, False, ZfsInfo()) is SlotHealth.FAILED


def test_truly_empty_bay_still_reads_empty() -> None:
    """The reorder must not reclassify genuinely empty bays."""
    assert classify("Not installed", False, False, ZfsInfo()) is SlotHealth.EMPTY
    assert classify("unknown", False, False, ZfsInfo()) is SlotHealth.EMPTY


def test_populated_bay_classification_is_unchanged() -> None:
    assert classify("OK", False, True, ZfsInfo()) is SlotHealth.OK
    assert classify("OK", True, True, ZfsInfo()) is SlotHealth.FAILED


# ------------------------------------- finding 2: hardware poll off the loop


async def test_poll_hardware_runs_enclosure_io_off_the_event_loop() -> None:
    """discover()/read_slots() take the cross-process enclosure lock, which
    busy-waits up to 30s; run on the loop thread that stalls every request,
    healthz included. Assert they execute on an executor thread instead."""
    ref = _ref()

    class RecordingBackend:
        def __init__(self) -> None:
            self.threads: list[threading.Thread] = []

        def discover(self) -> list[EnclosureRef]:
            self.threads.append(threading.current_thread())
            return [ref]

        def read_slots(self, got: EnclosureRef) -> list[SlotState]:
            assert got is ref
            self.threads.append(threading.current_thread())
            return [_slot(0, "sda")]

    backend = RecordingBackend()
    service = _service(backend=backend, settings=FakeSettings())
    await service.poll_hardware()

    loop_thread = threading.current_thread()
    assert len(backend.threads) == 2, "both discover() and read_slots() must have run"
    assert all(t is not loop_thread for t in backend.threads)
    # The executor hop must not change what lands in the caches.
    assert [r.logical_id for r in service.enclosures.value] == [STL3_ID]
    assert [s.ses_slot for s in service.slots.value[STL3_ID]] == [0]


async def test_poll_hardware_failure_still_degrades_the_cache() -> None:
    class BrokenBackend:
        def discover(self) -> list[EnclosureRef]:
            raise OSError("shelf unplugged")

    service = _service(backend=BrokenBackend(), settings=FakeSettings())
    await service.poll_hardware()
    assert "shelf unplugged" in (service.slots.last_error or "")


# ------------------------------ finding 3: AES-keyed SAS addresses in bays()


def test_stl3_capture_maps_every_bay_by_device_slot_number() -> None:
    mapping = _slot_sas_addresses(
        (FIXTURES / "sg_cf.txt").read_text(), (FIXTURES / "sg_aes.txt").read_text()
    )
    assert sorted(mapping) == list(range(15))
    assert mapping[0] == "0x5000cca0e0000002"
    assert mapping[14] == "0x5000cca0e00000e2"


def test_permuted_shelf_keys_addresses_by_device_slot_number() -> None:
    """The regression proper: element 0 sits in bay 4 on this shelf, so bay 4
    must show element 0's address. Element-index keying returned nothing for
    bay 4 and could pair other bays with the wrong drive's address. Element 2
    is an empty bay with no slot number and must stay unmapped; the expander
    block's address must not leak in."""
    mapping = _slot_sas_addresses(
        (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        (SYNTHETIC / "sg_aes_permuted.txt").read_text(),
    )
    assert mapping == {
        4: "0x5000aaaa00000001",
        1: "0x5000aaaa00000011",
        2: "0x5000aaaa00000031",
    }


#: A shelf whose firmware (wrongly) assigns the same device slot number to two
#: elements, each carrying its own SAS address. Hand-written like the IDENT
#: path's duplicate fixture (test_locate_mapping.py); the parser-level format
#: is validated against the real capture in test_ses_parser.py.
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
        phy index: 0
          SAS address: 0x5000aaaa00000001
          phy identifier: 0x0
      Element index: 1  eiioe=1
        Transport protocol: SAS
        number of phys: 1, not all phys: 1, device slot number: 2
        phy index: 0
          SAS address: 0x5000aaaa00000011
          phy identifier: 0x0
"""


def test_duplicate_slot_claims_yield_no_address() -> None:
    """Two elements claiming one slot number cannot be joined unambiguously;
    no address is shown rather than an arbitrary pick - the refusal policy
    the IDENT path already applies."""
    mapping = _slot_sas_addresses(
        (SYNTHETIC / "sg_cf_device_slot.txt").read_text(), DUPLICATE_SLOT_AES
    )
    assert mapping == {}


def _stl3_pages() -> dict[str, str]:
    return {
        "configuration": (FIXTURES / "sg_cf.txt").read_text(),
        "join": (FIXTURES / "sg_join.txt").read_text(),
        "additional_element_status": (FIXTURES / "sg_aes.txt").read_text(),
    }


async def test_poll_chassis_feeds_bay_sas_addresses_through_the_aes_map() -> None:
    """End to end on the permuted shelf: after a chassis poll, the bay at
    sysfs slot 4 must display element 0's address."""
    pages = {
        "configuration": (SYNTHETIC / "sg_cf_device_slot.txt").read_text(),
        # An empty join page is fine: telemetry elements are irrelevant here,
        # and the address map must not depend on them.
        "join": "",
        "additional_element_status": (SYNTHETIC / "sg_aes_permuted.txt").read_text(),
    }
    service = _service(ses=FakeSes(pages))
    service.enclosures.succeed([_ref(PERMUTED_ID)])
    service.slots.succeed({PERMUTED_ID: [_slot(4)]})

    await service.poll_chassis()

    (bay,) = service.bays(PERMUTED_ID)
    assert bay.disk.sas_address == "0x5000aaaa00000001"


async def test_aes_read_failure_drops_addresses_rather_than_serving_stale() -> None:
    """After a drive swap a stale map is the removed drive's address against
    the new drive, so a failed AES read clears the map instead of keeping it.
    The cf/join telemetry itself is unaffected and the error is surfaced."""
    ses = FakeSes(_stl3_pages())
    service = _service(ses=ses)
    service.enclosures.succeed([_ref()])

    await service.poll_chassis()
    assert service._sas_addresses(STL3_ID)[0] == "0x5000cca0e0000002"

    ses.fail.add("additional_element_status")
    await service.poll_chassis()
    assert service._sas_addresses(STL3_ID) == {}
    assert STL3_ID in service.chassis.value, "cf/join telemetry must survive an AES failure"
    assert "additional_element_status" in (service.chassis.last_error or "")


# ------------------------------------------- finding 4: same-name drive swap


def _bay_service(
    local: DiskIdentity, remote: DiskIdentity | None, zfs: ZfsInfo
) -> StateService:
    service = _service(disks=FakeDiskReader({"sdf": local}))
    service.enclosures.succeed([_ref()])
    service.slots.succeed({STL3_ID: [_slot(3, "sdf")]})
    if remote is not None:
        service.remote_disks.succeed({"sdf": remote})
    service.zfs.succeed({"sdf": zfs})
    return service


def test_swapped_drive_does_not_wear_previous_identity() -> None:
    """The kernel provably reuses block names (a replacement drive got the
    removed drive's ``sdf``). Until poll_truenas refreshes, the remote record
    and ZFS state under that name describe the REMOVED disk and must be
    dropped, not merged onto the new one."""
    local = DiskIdentity(serial="NEW123", wwn="0x5000cca000000002")
    remote = DiskIdentity(serial="OLD999", wwn="0x5000cca000000001", model="Old Model 4TB")
    zfs = ZfsInfo(pool="tank", vdev="raidz2-0", state=ZfsState.FAULTED)

    (bay,) = _bay_service(local, remote, zfs).bays(STL3_ID)

    assert bay.disk.serial == "NEW123"
    assert bay.disk.model is None, "the removed drive's model must not fill the gap"
    assert bay.zfs.pool is None
    assert bay.zfs.state is ZfsState.UNKNOWN
    # And the health must not be FAILED via the removed disk's FAULTED state.
    assert bay.health is SlotHealth.OK


def test_matching_identity_still_merges_and_attaches_zfs() -> None:
    """The conflict test must not break the normal, matching-identity path."""
    local = DiskIdentity(serial="SAME1", wwn="0x5000cca000000001")
    remote = DiskIdentity(serial="SAME1", wwn="0x5000cca000000001", model="Model 4TB")
    zfs = ZfsInfo(pool="tank", state=ZfsState.ONLINE)

    (bay,) = _bay_service(local, remote, zfs).bays(STL3_ID)

    assert bay.disk.model == "Model 4TB"
    assert bay.zfs.pool == "tank"
    assert bay.health is SlotHealth.OK


def test_identity_conflict_rules() -> None:
    a, b = "0x5000cca000000001", "0x5000cca000000002"
    # WWN is decisive when both sides carry one.
    assert _identity_conflict(DiskIdentity(wwn=a), DiskIdentity(wwn=b))
    assert not _identity_conflict(DiskIdentity(wwn=a), DiskIdentity(wwn=a))
    # Agreeing WWNs mean a serial difference is formatting, not a swap.
    assert not _identity_conflict(
        DiskIdentity(serial="ABC 123", wwn=a), DiskIdentity(serial="abc123", wwn=a)
    )
    # Serial is the fallback when either WWN is absent.
    assert _identity_conflict(DiskIdentity(serial="X"), DiskIdentity(serial="Y"))
    assert not _identity_conflict(DiskIdentity(serial="X"), DiskIdentity(serial="x "))
    # A field present on only one side proves nothing.
    assert not _identity_conflict(DiskIdentity(serial="X"), DiskIdentity())
    assert not _identity_conflict(DiskIdentity(), DiskIdentity(serial="Y", wwn=b))
