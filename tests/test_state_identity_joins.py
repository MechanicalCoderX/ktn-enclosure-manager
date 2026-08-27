"""Identity-join and poll-bookkeeping regressions in services/state.py.

StateService composes facts read on five different clocks - slots at 5s,
TrueNAS zfs/disks at 20s, SES/AES at 30s, SMART at 120s, sysfs disk identity
live - onto one Bay. Every defect here is the same root cause as the v1.5.4
wrong-bay IDENT mapping: joining sources of different ages without first
establishing that they describe the same disk.

Covered:

#3  The identity-conflict drop did not extend to sas_address, so at the exact
    moment the app had proven the bay's occupant changed, it still served the
    departed drive's SAS port address - the field an operator uses to
    cross-check a bay against the physical shelf.
#4  _identity_conflict answers False when either side carries no identifiers
    (S20: absence of data must not be treated as data), and DiskInfoReader
    returned a silently empty identity when its sysfs reads FAILED. The guard
    therefore switched itself off during a re-probe or on a disk whose
    attributes EIO - the window in which a same-name swap is most likely - and
    merge_identity then painted the departed drive's serial, pool and
    temperature alert onto the bay.
#5  _notify_health_changes ran bays() after every poll, so one transient join
    became an urgent phone alert naming the wrong drive, persisted to
    notify-state.json, followed by a "recovered".
#8  A failing discover() advanced no clock: the poll gate stayed permanently
    open and re-took the cross-process enclosure flock every second, while
    enclosures.last_error stayed null on the diagnostics page.
#9  poll_chassis stamped the cache fresh after reading nothing at all, so
    diagnostics reported last_success=<now> for telemetry of unbounded age.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ktnmgr.enclosure.disks import DiskIdentityUnreadable, DiskInfoReader
from ktnmgr.enclosure.ses import SesResult
from ktnmgr.models import (
    Bay,
    ChassisTelemetry,
    DiskIdentity,
    EnclosureRef,
    SlotHealth,
    SlotState,
    SmartInfo,
    ZfsInfo,
    ZfsState,
)
from ktnmgr.services.state import StateService

FIXTURES = Path(__file__).parent / "fixtures" / "ktn-stl3"

STL3_ID = "0x50060480aabbcc00"

#: The bay every join test uses, and the block name the kernel provably reuses
#: across a swap (see DiskInfoReader.read).
SES_SLOT = 3
DEVICE = "sdf"

OLD_DRIVE_SAS = "0x5000cca0e0000042"


# ------------------------------------------------------------------- fixtures


def _ref(sg_device: str | None = "/dev/sg16") -> EnclosureRef:
    return EnclosureRef(
        logical_id=STL3_ID,
        vendor="EMC",
        product="ESES Enclosure",
        revision="0001",
        scsi_address="1:0:15:0",
        sysfs_path="/sys/class/enclosure/1:0:15:0",
        sg_device=sg_device,
        slot_count=15,
    )


def _slot(
    ses_slot: int = SES_SLOT,
    block_device: str | None = DEVICE,
    status: str = "OK",
    fault: bool = False,
) -> SlotState:
    return SlotState(
        ses_slot=ses_slot,
        display_bay=ses_slot + 1,
        status=status,
        fault=fault,
        block_device=block_device,
        sysfs_path=f"/sys/class/enclosure/1:0:15:0/{ses_slot}",
    )


class FakeSettings:
    poll_slots_seconds = 5.0
    poll_truenas_seconds = 20.0
    poll_ses_seconds = 30.0
    poll_smart_seconds = 120.0
    # Only read by diagnostics(), which test_discover_failure_records_the_error_
    # on_the_enclosure_cache below exercises end to end.
    truenas_url = ""
    truenas_verify_tls = True
    sysfs_root = "/sys"
    ident_helper_socket = None

    def allowed_enclosures(self) -> set[str]:
        return set()


class FakeIdent:
    def describe(
        self, enclosure_id: str, ses_slot: int, locate: bool, observed_at: float
    ) -> tuple[bool, None, None]:
        return (locate, None, None)


class MappingDiskReader:
    """Serves canned identities, for tests about the join rather than sysfs.

    Every unreadable-identity case goes through a real DiskInfoReader over a
    real (broken) sysfs tree instead, so no test here can pass by agreeing with
    a fake about what "unreadable" means.
    """

    def __init__(self, identities: dict[str, DiskIdentity]) -> None:
        self.identities = identities

    def read(self, name: str | None) -> DiskIdentity:
        if not name:
            return DiskIdentity()
        return self.identities.get(name, DiskIdentity())


class UnavailableSes:
    binary = "sg_ses"

    def available(self) -> bool:
        return False

    def version(self) -> str | None:
        # diagnostics() calls this unconditionally alongside available(); an
        # uninstalled binary has no version to report.
        return None


class RefusingSes:
    """Available, but any read is a test failure - nothing should be read."""

    binary = "sg_ses"

    def available(self) -> bool:
        return True

    def read_for(self, ref: Any, page: str) -> SesResult:
        raise AssertionError(f"read_for({page}) must not run with nothing to read")


class FixtureSes:
    binary = "sg_ses"

    def available(self) -> bool:
        return True

    def read_for(self, ref: Any, page: str) -> SesResult:
        name = {
            "configuration": "sg_cf.txt",
            "join": "sg_join.txt",
            "additional_element_status": "sg_aes.txt",
        }[page]
        return SesResult(page=page, stdout=(FIXTURES / name).read_text(), returncode=0)


class RecordingNotifier:
    """Captures exactly which bays reach HealthNotifier.evaluate()."""

    enabled = True

    def __init__(self) -> None:
        self.calls: list[list[Bay]] = []

    async def evaluate(self, bays: list[Bay]) -> None:
        self.calls.append(list(bays))


def _service(
    disks: Any = None,
    backend: Any = None,
    ses: Any = None,
    notifier: Any = None,
) -> StateService:
    return StateService(
        settings=FakeSettings(),
        backend=backend,
        disks=disks if disks is not None else MappingDiskReader({}),
        ses=ses if ses is not None else UnavailableSes(),
        ident=FakeIdent(),
        truenas=None,
        notifier=notifier,
    )


def _joined(
    disks: Any,
    *,
    remote: DiskIdentity | None = None,
    zfs: ZfsInfo | None = None,
    smart: SmartInfo | None = None,
    sas_address: str | None = OLD_DRIVE_SAS,
    slots: list[SlotState] | None = None,
    notifier: Any = None,
) -> StateService:
    """A service holding one bay plus the remote state keyed by its block name."""
    service = _service(disks=disks, notifier=notifier)
    service.enclosures.succeed([_ref()])
    service.slots.succeed({STL3_ID: slots if slots is not None else [_slot()]})
    if remote is not None:
        service.remote_disks.succeed({DEVICE: remote})
    if zfs is not None:
        service.zfs.succeed({DEVICE: zfs})
    if smart is not None:
        service.smart.succeed({DEVICE: smart})
    if sas_address is not None:
        service._sas_by_slot[STL3_ID] = {SES_SLOT: sas_address}
    return service


def _sysfs_disk(
    root: Path,
    name: str,
    *,
    wwid: str | None = None,
    serial: str | None = None,
    model: str | None = "New Model 4TB",
    unreadable: tuple[str, ...] = (),
) -> None:
    """Build a minimal /sys/block/<name> tree.

    An attribute listed in ``unreadable`` is created as a *directory*, so
    read_text/read_bytes raise IsADirectoryError - an OSError that is not
    ENOENT, which is what a disk answering EIO looks like to this reader.
    An attribute simply omitted is ENOENT: the disk declaring it has none.
    """
    block = root / "block" / name
    (block / "queue").mkdir(parents=True)
    (block / "size").write_text("7814037168\n")
    (block / "queue" / "rotational").write_text("1\n")
    device = block / "device"
    device.mkdir()
    if model is not None:
        (device / "model").write_text(f"{model}\n")
    for attr, value in (("wwid", wwid), ("serial", serial)):
        if attr in unreadable:
            (device / attr).mkdir()
        elif value is not None:
            (device / attr).write_text(f"{value}\n")
    if "vpd_pg80" in unreadable:
        (device / "vpd_pg80").mkdir()


# ------------------------------- #3: the conflict drop must include the address


def test_conflicting_identity_drops_the_previous_drives_sas_address() -> None:
    """A swap that reuses the block name is proven by disagreeing identifiers.

    At that instant every field keyed to the departed drive must go, and the
    SAS port address is the one an operator reads off the shelf to confirm
    which physical drive a bay is. Showing the previous drive's address is the
    same class of error as showing its serial, and it survived the v1.5.4 drop
    because it is stamped from the slot-keyed AES map after the drop runs.
    """
    service = _joined(
        MappingDiskReader({DEVICE: DiskIdentity(serial="NEW123", wwn="0x5000cca000000002")}),
        remote=DiskIdentity(serial="OLD999", wwn="0x5000cca000000001"),
    )

    (bay,) = service.bays(STL3_ID)

    assert bay.disk.serial == "NEW123"
    assert bay.disk.sas_address is None


def test_agreeing_identity_still_shows_the_sas_address() -> None:
    """The control: the address must still be served on the normal path."""
    same = DiskIdentity(serial="SAME1", wwn="0x5000cca000000001")
    service = _joined(MappingDiskReader({DEVICE: same}), remote=same)

    (bay,) = service.bays(STL3_ID)

    assert bay.disk.sas_address == OLD_DRIVE_SAS


# ------------------------- #4: an unreadable identity is not an empty identity


def test_unreadable_identifiers_raise_rather_than_returning_empty(tmp_path: Path) -> None:
    """The reader's own contract. Both identifier sources fail; the model does
    not, which is exactly the real shape - model and vendor are kernel-cached
    while vpd_pg80 is a live INQUIRY to a drive that has stopped answering."""
    _sysfs_disk(tmp_path, DEVICE, unreadable=("wwid", "vpd_pg80", "serial"))

    with pytest.raises(DiskIdentityUnreadable):
        DiskInfoReader(sysfs_root=tmp_path).read(DEVICE)


def test_absent_identifier_attributes_still_return_an_empty_identity(
    tmp_path: Path,
) -> None:
    """S20's own case, and the line the fix must not cross: a driver that
    exposes no wwid and no serial has told us something true about the disk.
    That is absence, not failure, and must stay a plain empty identity."""
    _sysfs_disk(tmp_path, DEVICE)

    identity = DiskInfoReader(sysfs_root=tmp_path).read(DEVICE)

    assert identity.serial is None
    assert identity.wwn is None
    assert identity.model == "New Model 4TB"


def test_one_readable_identifier_is_enough_to_identify_the_disk(
    tmp_path: Path,
) -> None:
    """Only a total loss of identity is unreadable. A serial that arrives
    while wwid errors still lets _identity_conflict do its job, so raising
    there would throw away a usable correlation."""
    _sysfs_disk(tmp_path, DEVICE, serial="NEW123", unreadable=("wwid", "vpd_pg80"))

    identity = DiskInfoReader(sysfs_root=tmp_path).read(DEVICE)

    assert identity.serial == "NEW123"
    assert identity.wwn is None


def test_an_unreadable_disk_is_not_served_from_the_identity_cache(
    tmp_path: Path,
) -> None:
    """The cache is validated by re-reading wwid precisely because /dev/sdX is
    reused. When that read is the one that failed, the cached entry cannot be
    revalidated and must not be served - a swap is the likeliest reason a disk
    is mid-re-probe in the first place."""
    _sysfs_disk(tmp_path, DEVICE, wwid="naa.5000cca000000001", serial="OLD999")
    reader = DiskInfoReader(sysfs_root=tmp_path)
    assert reader.read(DEVICE).serial == "OLD999"

    for attr in ("wwid", "serial"):
        (tmp_path / "block" / DEVICE / "device" / attr).unlink()
        (tmp_path / "block" / DEVICE / "device" / attr).mkdir()

    with pytest.raises(DiskIdentityUnreadable):
        reader.read(DEVICE)


def test_unreadable_identity_does_not_take_the_previous_drives_state(
    tmp_path: Path,
) -> None:
    """The probe result in full, end to end through the real reader.

    A replacement drive holds the removed drive's ``sdf`` and its identity
    cannot be read yet. The <=20s TrueNAS caches still describe the removed
    drive under that name: FAULTED, a pool membership, a 71C alert, serial
    OLD999. Before the fix the empty identity made the conflict guard answer
    "no conflict", merge_identity backfilled OLD999, and the bay rendered
    FAILED - a wrong-drive failure report, which is the single outcome this
    application exists to prevent.
    """
    _sysfs_disk(tmp_path, DEVICE, unreadable=("wwid", "vpd_pg80", "serial"))
    service = _joined(
        DiskInfoReader(sysfs_root=tmp_path),
        remote=DiskIdentity(serial="OLD999", wwn="0x5000cca000000001", model="Old 4TB"),
        zfs=ZfsInfo(pool="tank", vdev="raidz2-0", state=ZfsState.FAULTED),
        smart=SmartInfo(temperature_c=71.0, over_temperature=True, alert="overheating"),
    )

    (bay,) = service.bays(STL3_ID)

    # The IDENTITY is withheld: nothing may claim to know which drive this is.
    assert bay.disk.serial is None, "the departed drive's serial must not be backfilled"
    assert bay.disk.model is None
    # The FAILURE SIGNAL is kept. An unreadable identity says nothing about
    # whether the bay is in trouble, and treating "unknown occupant" as "no
    # failure to report" is how a shelf goes quiet exactly when it should not:
    # an EACCES on the container's disk access takes this shape on every bay at
    # once. A false alarm naming no drive is recoverable; silence is not.
    assert bay.health is SlotHealth.FAILED, "a FAULTED bay must not render healthy"
    assert bay.zfs.state is ZfsState.FAULTED
    assert bay.smart.over_temperature is True


def test_unreadable_identity_drops_the_sas_address_too(tmp_path: Path) -> None:
    """Same rule as #3 and for the same reason: the AES map is up to 30s old,
    so it describes whichever drive was in the bay when it was read. Without a
    live identity there is nothing to say it is still that drive."""
    _sysfs_disk(tmp_path, DEVICE, unreadable=("wwid", "vpd_pg80", "serial"))

    (bay,) = _joined(DiskInfoReader(sysfs_root=tmp_path)).bays(STL3_ID)

    assert bay.disk.sas_address is None


def test_a_disk_declaring_no_identifiers_still_merges_remote_state(
    tmp_path: Path,
) -> None:
    """The S20 guard rail at the join, not just at the reader. A disk with no
    wwid and no serial has proven nothing against the TrueNAS record, and the
    established behaviour is to merge it - the fix must narrow itself to reads
    that FAILED, or every such disk silently loses its pool membership."""
    _sysfs_disk(tmp_path, DEVICE)
    service = _joined(
        DiskInfoReader(sysfs_root=tmp_path),
        remote=DiskIdentity(serial="ABC123", wwn="0x5000cca000000001"),
        zfs=ZfsInfo(pool="tank", state=ZfsState.ONLINE),
    )

    (bay,) = service.bays(STL3_ID)

    assert bay.disk.serial == "ABC123"
    assert bay.zfs.pool == "tank"
    assert bay.disk.sas_address == OLD_DRIVE_SAS


# ----------------------------------------------- #5: notifier amplification


async def test_notifier_still_judges_a_bay_whose_identity_is_unreadable(
    tmp_path: Path,
) -> None:
    """The incident's cause was the composition, not the alert.

    The original bug sent an urgent "Bay 4 FAILED" naming the DEPARTED drive's
    serial, pool and 71C. The tempting fix - withhold any bay whose identity
    could not be read - trades a wrong-drive alert for no alert at all, and an
    EACCES on the container's disk access puts every bay in that state at once.

    So the bay is still judged; what it no longer carries is an identity the
    reader could not vouch for. The alert says which BAY to look at, and claims
    nothing about which drive is in it.
    """
    _sysfs_disk(tmp_path, DEVICE, unreadable=("wwid", "vpd_pg80", "serial"))
    _sysfs_disk(tmp_path, "sdg", wwid="naa.5000cca000000009", serial="FINE1")
    notifier = RecordingNotifier()
    service = _joined(
        DiskInfoReader(sysfs_root=tmp_path),
        remote=DiskIdentity(serial="OLD999", wwn="0x5000cca000000001"),
        zfs=ZfsInfo(pool="tank", vdev="raidz2-0", state=ZfsState.FAULTED),
        smart=SmartInfo(temperature_c=71.0, over_temperature=True),
        slots=[_slot(), _slot(ses_slot=4, block_device="sdg")],
        notifier=notifier,
    )

    await service._notify_health_changes()

    (seen,) = notifier.calls
    assert sorted(bay.ses_slot for bay in seen) == [SES_SLOT, 4], (
        "an unconfirmed bay must still be judged - silence is the worse failure"
    )
    unconfirmed = next(bay for bay in seen if bay.ses_slot == SES_SLOT)
    assert unconfirmed.health is SlotHealth.FAILED, "the FAULTED state must survive"
    assert unconfirmed.disk.serial is None, "but it may not name the departed drive"


async def test_notifier_still_sees_an_enclosure_asserted_fault(tmp_path: Path) -> None:
    """The floor under the withholding rule, and the reason it is a floor
    rather than a blanket skip.

    A dying drive is exactly the one whose sysfs attributes stop answering, so
    a rule keyed on unreadable identity would go quiet precisely as the
    hardware got worse. The shelf's own fault bit is read from the enclosure
    and not from that drive, so an unreadable join can neither fabricate it nor
    make it flap, and it still reaches the notifier.

    This one passes before the fix as well - it exists to fail if the fix is
    ever widened into suppressing the alerts it was supposed to make truthful.
    """
    _sysfs_disk(tmp_path, DEVICE, unreadable=("wwid", "vpd_pg80", "serial"))
    notifier = RecordingNotifier()
    service = _joined(
        DiskInfoReader(sysfs_root=tmp_path),
        slots=[_slot(fault=True)],
        notifier=notifier,
    )

    await service._notify_health_changes()

    (seen,) = notifier.calls
    assert [bay.ses_slot for bay in seen] == [SES_SLOT]
    assert seen[0].health is SlotHealth.FAILED


async def test_notifier_sees_every_bay_whose_identity_resolved() -> None:
    """The control. Without it a broken harness would look like a pass, since
    _notify_health_changes swallows every exception by design."""
    notifier = RecordingNotifier()
    service = _joined(
        MappingDiskReader({DEVICE: DiskIdentity(serial="NEW123")}),
        slots=[_slot(), _slot(ses_slot=4, block_device=None)],
        notifier=notifier,
    )

    await service._notify_health_changes()

    (seen,) = notifier.calls
    assert [bay.ses_slot for bay in seen] == [SES_SLOT, 4]


# -------------------------------------- #8: a failing discover advances nothing


class BrokenBackend:
    def __init__(self) -> None:
        self.calls = 0

    def discover(self) -> list[EnclosureRef]:
        self.calls += 1
        raise OSError("no enclosure device found")


async def test_discover_failure_records_the_error_on_the_enclosure_cache() -> None:
    """enclosures.last_error stayed null forever, so the diagnostics page (S35)
    showed the failing source as one that had simply never been polled."""
    service = _service(backend=BrokenBackend())

    await service.poll_hardware()

    assert "no enclosure device found" in (service.enclosures.last_error or "")
    assert service.enclosures.last_attempt_at is not None
    assert service.enclosures.updated_at is None, "a failure is not a success"
    # The recorded error must actually reach the diagnostics payload (§35) -
    # StateService.enclosures.fail() alone is not enough if nothing in
    # diagnostics() ever reads it back. Same interval as `slots`: both are
    # stamped by the same poll_hardware() call.
    polling = service.diagnostics()["polling"]
    assert "enclosures" in polling, "diagnostics() must expose an 'enclosures' polling entry"
    assert "no enclosure device found" in (polling["enclosures"]["last_error"] or "")
    assert polling["enclosures"]["last_success"] is None


async def test_discover_failure_leaves_nothing_immediately_due() -> None:
    """The hot loop stated as an invariant: after a failed attempt, neither
    clock the poll gate consults may still read as due. Both used to - the
    enclosure cache because nothing stamped it, the slot cache because the gate
    tested it for emptiness rather than freshness."""
    service = _service(backend=BrokenBackend())

    await service.poll_hardware()

    assert not service.enclosures.due(FakeSettings.poll_slots_seconds)
    assert not service.slots.due(FakeSettings.poll_slots_seconds)


async def test_a_failing_shelf_is_retried_on_the_interval_not_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consequence that made this urgent: every retry takes the
    cross-process enclosure flock, which the IDENT helper also needs, so an
    unplugged shelf became sustained contention against the one operation that
    must not be starved. With the loop's own sleep collapsed, a 5s poll
    interval must still yield exactly one attempt."""
    real_sleep = asyncio.sleep

    async def instant(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant)

    backend = BrokenBackend()
    service = _service(backend=backend)
    await service.poll_hardware()
    assert backend.calls == 1

    task = asyncio.create_task(service._loop())
    await real_sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.calls == 1, "poll_slots_seconds has not elapsed; nothing was due"


class SlotlessBackend:
    """discover() answers; read_slots() is the call that fails."""

    def discover(self) -> list[EnclosureRef]:
        return [_ref()]

    def read_slots(self, ref: EnclosureRef) -> list[SlotState]:
        raise OSError("slot read timed out")


async def test_read_slots_failure_leaves_the_enclosure_cache_fresh() -> None:
    """The control on the fix's scope. discover() did answer, so the enclosure
    cache is genuinely fresh and only the slot cache degrades (S37); marking
    both failed would have thrown away a good reading."""
    service = _service(backend=SlotlessBackend())

    await service.poll_hardware()

    assert service.enclosures.last_error is None
    assert service.enclosures.updated_at is not None
    assert "slot read timed out" in (service.slots.last_error or "")


# ------------------------------- #9: a chassis poll that read nothing at all


async def test_poll_chassis_with_no_enclosures_does_not_claim_a_fresh_read() -> None:
    """`collected` starts as the previous cache handed back to itself, so it is
    non-empty after any past success and cannot answer "did we read anything".
    Stamping it moved updated_at to now and reported last_error=null for
    telemetry of unbounded age."""
    service = _service(ses=RefusingSes())
    service.chassis.succeed({STL3_ID: ChassisTelemetry(enclosure_id=STL3_ID)})
    stamped = service.chassis.updated_at
    service._sas_by_slot[STL3_ID] = {SES_SLOT: OLD_DRIVE_SAS}

    await service.poll_chassis()

    assert service.chassis.updated_at == stamped, "nothing was read; nothing is fresh"
    assert service.chassis.last_error is not None
    assert service.chassis.last_attempt_at is not None
    # No enclosure contributed a reading, so no address map can be rebuilt -
    # the honest-absence rule the SesError paths already apply.
    assert service._sas_addresses(STL3_ID) == {}
    # And the RETAINED telemetry itself must say it is stale - routes.chassis()
    # serves service.chassis.value verbatim whenever an entry exists and only
    # falls back to chassis.last_error when there is none, so an unmarked
    # object here would keep serving fan speeds and temperatures of unbounded
    # age with stale=false on the one page an operator actually looks at.
    telemetry = service.chassis.value[STL3_ID]
    assert telemetry.stale is True
    assert telemetry.error is not None


async def test_poll_chassis_with_no_sg_node_does_not_claim_a_fresh_read() -> None:
    """Same defect reached the other way: an attached enclosure that lost its
    sg node is skipped by the loop, so nothing is read and the carried-forward
    cache was again stamped as a successful poll."""
    service = _service(ses=RefusingSes())
    service.enclosures.succeed([_ref(sg_device=None)])
    service.chassis.succeed({STL3_ID: ChassisTelemetry(enclosure_id=STL3_ID)})
    stamped = service.chassis.updated_at

    await service.poll_chassis()

    assert service.chassis.updated_at == stamped
    assert service.chassis.last_error is not None
    # Same as the no-enclosures case: an enclosure that is still attached but
    # has lost its sg node must not go on serving old telemetry as current.
    telemetry = service.chassis.value[STL3_ID]
    assert telemetry.stale is True
    assert telemetry.error is not None


async def test_a_real_chassis_read_still_stamps_the_cache_fresh() -> None:
    """The control: an actual reading must go on reporting itself as one."""
    service = _service(ses=FixtureSes())
    service.enclosures.succeed([_ref()])

    await service.poll_chassis()

    assert service.chassis.last_error is None
    assert service.chassis.updated_at is not None
    assert STL3_ID in service.chassis.value
    assert service._sas_addresses(STL3_ID)[0] == "0x5000cca0e0000002"
    assert service.chassis.value[STL3_ID].stale is False, (
        "a genuine reading must not be left marked stale by an earlier failure"
    )


async def test_a_shelf_that_cannot_read_any_identity_still_reports_every_failure(
    tmp_path: Path,
) -> None:
    """The failure mode that makes withholding unacceptable, at shelf scale.

    An EACCES on the container's disk access - a permission regression, not a
    hardware event - makes every identity read fail at once. If an unconfirmed
    occupant suppressed the bay's failure signals, a shelf of dying drives
    would render healthy and send nothing: the application would be at its
    quietest exactly when it should be loudest. Fifteen bays, all FAULTED, all
    unreadable: fifteen must still be judged failed.
    """
    slots = [_slot(ses_slot=n, block_device=f"sd{chr(ord('a') + n)}") for n in range(15)]
    for slot in slots:
        _sysfs_disk(tmp_path, slot.block_device, unreadable=("wwid", "vpd_pg80", "serial"))

    notifier = RecordingNotifier()
    service = _service(disks=DiskInfoReader(sysfs_root=tmp_path), notifier=notifier)
    service.enclosures.succeed([_ref()])
    service.slots.succeed({STL3_ID: slots})
    service.zfs.succeed(
        {slot.block_device: ZfsInfo(pool="tank", state=ZfsState.FAULTED) for slot in slots}
    )

    bays = service.bays(STL3_ID)
    assert [b.health for b in bays] == [SlotHealth.FAILED] * 15
    assert all(b.disk.serial is None for b in bays), "no bay may claim an identity"

    await service._notify_health_changes()
    (seen,) = notifier.calls
    assert len(seen) == 15, "a shelf-wide identity outage must not silence alerting"
