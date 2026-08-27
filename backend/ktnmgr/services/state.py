"""Polling and caching.

One backend poll serves every connected UI session (spec §29). Each source has
its own interval and its own last-good cache, so a slow or failing source
degrades that section only - the bay map keeps updating from sysfs even when
TrueNAS is unreachable or sg_ses times out (§37).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from ktnmgr import __version__
from ktnmgr.config import Settings
from ktnmgr.enclosure.disks import DiskIdentityUnreadable, DiskInfoReader
from ktnmgr.enclosure.ses import SesError, SesRunner
from ktnmgr.enclosure.ses_parser import (
    BAY_ELEMENT_TYPES,
    array_slot_type_index,
    build_telemetry,
    parse_additional_element_status,
    parse_configuration,
)
from ktnmgr.enclosure.sysfs import EnclosureNotFoundError, SysfsEnclosureBackend
from ktnmgr.models import (
    Bay,
    ChassisTelemetry,
    DiskIdentity,
    EnclosureRef,
    SlotHealth,
    SmartInfo,
    ZfsInfo,
    ZfsState,
)
from ktnmgr.services.ident import IdentManager
from ktnmgr.services.notify import HealthNotifier
from ktnmgr.truenas.client import TrueNASClient, TrueNASError
from ktnmgr.truenas.correlate import (
    build_disk_index,
    build_smart_index,
    build_zfs_index,
    merge_identity,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Verbatim sysfs slot statuses in which the enclosure is itself calling the
#: bay failed. Kept next to the predicate below so the two readers of this list
#: - health classification and the notifier's floor - cannot drift apart.
_ENCLOSURE_FAULT_STATUSES = ("critical", "unrecoverable")


@dataclass
class Cached(Generic[T]):
    """Last-good value plus the freshness metadata the diagnostics page shows."""

    value: T
    updated_at: datetime | None = None
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    _monotonic: float = field(default=0.0, repr=False)
    #: Monotonic instant the data in `value` was READ, which is not when it was
    #: cached: a poll samples the hardware and only then stores the result, and
    #: for the slot cache the gap between the two is where a concurrent IDENT
    #: write lands (IdentManager.describe). `updated_at` answers "how fresh is
    #: this cache" for the diagnostics page; this answers "how old is the world
    #: it describes", which is the only question an ordering can be built on.
    observed_monotonic: float = field(default=0.0, repr=False)

    def due(self, interval: float) -> bool:
        return (time.monotonic() - self._monotonic) >= interval

    def succeed(self, value: T, observed_at: float | None = None) -> None:
        self.value = value
        self.updated_at = datetime.now(UTC)
        self.last_attempt_at = self.updated_at
        self.last_error = None
        self._monotonic = time.monotonic()
        # A caller that does not say when it sampled is taken to have sampled
        # now. Every source in fact samples a little before it stores, but the
        # difference only matters where something is ordered against the
        # reading, and the slot cache - which IdentManager.describe orders
        # against its own writes - is the one source that passes its real
        # sampling instant.
        self.observed_monotonic = self._monotonic if observed_at is None else observed_at

    def fail(self, error: str) -> None:
        self.last_attempt_at = datetime.now(UTC)
        self.last_error = error
        self._monotonic = time.monotonic()


def _enclosure_asserts_failure(status: str, fault: bool) -> bool:
    """True when the SHELF is calling this bay failed.

    Both inputs come from the enclosure's own sysfs/SES status, so this verdict
    is independent of the disk in the bay and of any TrueNAS record keyed by a
    transient block name. That independence is the point: it is still true when
    the disk itself has stopped answering, which is exactly when every other
    signal about the bay becomes unusable (§20).
    """
    return fault or (status or "").strip().lower() in _ENCLOSURE_FAULT_STATUSES


def classify(
    status: str,
    fault: bool,
    has_device: bool,
    zfs: ZfsInfo,
    smart: SmartInfo | None = None,
) -> SlotHealth:
    """Derive a bay's health. Text and icons carry this in the UI, not colour (§24).

    A TrueNAS temperature alert is a warning, not a failure: the disk is still
    serving data, but it is the one to look at. It ranks below any ZFS fault so
    a hot *and* faulted disk still reads as failed.
    """
    normalised = (status or "").strip().lower()
    # Fault outranks emptiness, so it is tested first. A drive can fail hard
    # enough that the kernel deletes its SCSI device - the bay then exposes no
    # block device - while the enclosure still asserts the slot's fault
    # indicator or reports Critical/Unrecoverable. Testing has_device first
    # rendered exactly that bay, the one most in need of attention, as EMPTY
    # on the shelf map.
    if _enclosure_asserts_failure(status, fault):
        return SlotHealth.FAILED
    if not has_device:
        return SlotHealth.EMPTY
    if zfs.state in (ZfsState.FAULTED, ZfsState.UNAVAIL, ZfsState.REMOVED):
        return SlotHealth.FAILED
    if zfs.state is ZfsState.DEGRADED or zfs.resilvering:
        return SlotHealth.WARNING
    if any((zfs.read_errors, zfs.write_errors, zfs.checksum_errors)):
        return SlotHealth.WARNING
    if smart is not None and smart.over_temperature:
        return SlotHealth.WARNING
    if normalised in ("noncritical", "non-critical", "warning"):
        return SlotHealth.WARNING
    if normalised == "ok":
        return SlotHealth.OK
    return SlotHealth.UNKNOWN


def _identity_conflict(local: DiskIdentity, remote: DiskIdentity) -> bool:
    """True when sysfs and the cached TrueNAS record describe different disks.

    The remote index is keyed by transient block name and refreshed only every
    poll_truenas interval, and the kernel provably reuses names: on the
    validation system a replacement drive was assigned the same ``sdf`` the
    removed drive had held (see DiskInfoReader.read). For up to one interval
    the removed disk's TrueNAS record therefore wears the new disk's name.

    WWN is compared first and is decisive when both sides carry one: it is the
    globally unique node identifier, both sides normalise it to the same
    ``0x...`` form, and agreeing WWNs mean any serial difference is formatting
    (VPD pg80 vs. disk.query), not a different disk. Serial is the fallback
    when either WWN is absent. A field present on only one side proves
    nothing, so it never counts as a conflict (§20: absence of data must not
    be treated as data).
    """
    if local.wwn and remote.wwn:
        return local.wwn.strip().lower() != remote.wwn.strip().lower()
    if local.serial and remote.serial:
        return local.serial.strip().lower() != remote.serial.strip().lower()
    return False


def _notifiable(bay: Bay) -> bool:
    """Whether a composed bay may drive a health notification this cycle.

    Every bay may. This function exists to record why there is no exception,
    because the obvious-looking one is a trap that was briefly implemented
    here.

    The incident this guards against was an urgent "Bay 4 FAILED" naming
    another drive's serial, pool and 71C, persisted to notify-state.json and
    followed by a "recovered" once the join settled. The tempting fix was to
    withhold any bay whose live identity read failed. It is the wrong lever:
    withholding is indistinguishable from silence at the moment the hardware
    is worst, and an EACCES on the container's disk access takes exactly that
    shape across every bay at once - fifteen failing drives, nothing sent.

    The cause was never the alert; it was the composition. _compose_bays now
    withholds the *identity* it cannot vouch for while keeping the failure
    signals that describe the bay, so an alert about an unconfirmed occupant
    names no drive it cannot prove is there - and still tells the operator
    which bay to open the shelf and look at. Alerting must not get quieter as
    the hardware gets worse (§20).
    """
    return True


class StateService:
    """Owns all polling and exposes composed views to the API layer."""

    def __init__(
        self,
        settings: Settings,
        backend: SysfsEnclosureBackend,
        disks: DiskInfoReader,
        ses: SesRunner,
        ident: IdentManager,
        truenas: TrueNASClient | None,
        notifier: HealthNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.disk_reader = disks
        self.ses = ses
        self.ident = ident
        self.truenas = truenas
        self.notifier = notifier

        self.enclosures: Cached[list[EnclosureRef]] = Cached(value=[])
        self.slots: Cached[dict[str, list[Any]]] = Cached(value={})
        self.zfs: Cached[dict[str, ZfsInfo]] = Cached(value={})
        self.remote_disks: Cached[dict[str, DiskIdentity]] = Cached(value={})
        self.smart: Cached[dict[str, SmartInfo]] = Cached(value={})
        self.chassis: Cached[dict[str, ChassisTelemetry]] = Cached(value={})
        self.system_info: Cached[dict[str, Any]] = Cached(value={})

        # logical id -> {AES device slot number -> drive SAS address}. Kept
        # separate from the chassis telemetry cache because the two answer in
        # different coordinate systems: telemetry elements are keyed by
        # element index, while bays() looks addresses up by the sysfs slot
        # number, which the kernel fills from the AES page's device slot
        # number. See poll_chassis and _slot_sas_addresses.
        self._sas_by_slot: dict[str, dict[int, str]] = {}

        self._task: asyncio.Task[None] | None = None
        self._started_at = datetime.now(UTC)

    # --------------------------------------------------------------- polling

    async def poll_hardware(self) -> None:
        """sysfs: cheap, frequent, and the only source the bay map truly needs.

        Cheap does not mean non-blocking. discover() and read_slots() take the
        cross-process enclosure lock, which busy-waits up to 30s when the
        helper holds it (enclosure/access.py), and the slot reads themselves
        make the kernel ses driver issue diagnostics to the shelf. Run inline
        on the event loop, a wedged shelf or a stuck lock holder therefore
        froze the entire HTTP surface - healthz included - instead of merely
        degrading the slot cache, so both calls go through the executor
        exactly as poll_chassis's sg_ses reads do.
        """
        loop = asyncio.get_running_loop()
        # Stamped before the first executor hop, so it is the earliest instant
        # any reading below could have been taken. A poll is not instantaneous
        # - discover() and the per-enclosure read_slots() calls each queue
        # behind the cross-process enclosure lock - and an IDENT write can be
        # verified while this one is in flight, in which case the readings it
        # is about to cache are already superseded. Being early here is the
        # safe direction: it can only make bays() distrust a reading that
        # genuinely straddled one of our own writes.
        observed_at = time.monotonic()
        discovered_ok = False
        try:
            discovered = await loop.run_in_executor(None, self.backend.discover)
            found = [e for e in discovered if self._allowed(e.logical_id)]
            self.enclosures.succeed(found)
            discovered_ok = True
            slots = {
                ref.logical_id: await loop.run_in_executor(None, self.backend.read_slots, ref)
                for ref in found
            }
            self.slots.succeed(slots, observed_at=observed_at)
        except OSError as exc:
            self.slots.fail(str(exc))
            if not discovered_ok:
                # A failed discover() used to leave the enclosure cache
                # untouched, which cost twice. Its monotonic stamp never moved,
                # so _loop found it perpetually due and re-entered this method
                # every tick - each attempt taking the cross-process enclosure
                # flock that the IDENT helper also needs, turning an unplugged
                # shelf into contention against the one operation that must not
                # be starved. And last_error stayed None forever - not merely
                # undisplayed but never recorded, so no amount of looking at
                # the diagnostics block (§35) could distinguish a shelf that
                # was failing to discover from one that had simply never been
                # polled. Recording the attempt fixes both; note that the
                # `polling` block does not yet carry an `enclosures` entry, so
                # surfacing it is a separate, API-visible decision.
                # read_slots() failing is a different story: discover()
                # did answer, so the enclosure cache is genuinely fresh and only
                # the slot cache degrades (§37).
                self.enclosures.fail(str(exc))

    def _allowed(self, logical_id: str) -> bool:
        allowlist = self.settings.allowed_enclosures()
        return not allowlist or logical_id.lower() in allowlist

    async def poll_truenas(self) -> None:
        if self.truenas is None:
            return
        try:
            pools = await self.truenas.pools()
            records = await self.truenas.disks()
            self.zfs.succeed(build_zfs_index(pools))
            self.remote_disks.succeed(build_disk_index(records))
        except (TrueNASError, OSError) as exc:
            self.zfs.fail(str(exc))
            self.remote_disks.fail(str(exc))

    async def poll_smart(self) -> None:
        if self.truenas is None:
            return
        try:
            temperatures = await self.truenas.temperatures()
            # Ask only about disks actually in a bay. The appliance requires an
            # explicit name list, and there is no reason to ask about the boot
            # device or anything outside the shelf.
            #
            # This list is derived from the slot cache, which is up to
            # poll_slots_seconds old, and that is deliberately left alone. The
            # staleness can only make the list *incomplete*: a drive that has
            # since changed block name is asked about under a name that no
            # longer resolves, so the appliance returns nothing for it and the
            # bay shows no temperature for a cycle. It can never make the list
            # wrong, because build_smart_index keys results by the same block
            # name and _compose_bays independently re-checks identity before
            # attaching anything keyed that way (§20). Absent-for-one-cycle is
            # the failure mode this application prefers over a reading
            # attributed to the wrong drive, so no ordering check is warranted
            # here - unlike the slot cache's IDENT join, where the stale value
            # would have been *served* rather than merely omitted.
            names = sorted(
                {
                    slot.block_device
                    for slots in self.slots.value.values()
                    for slot in slots
                    if slot.block_device
                }
            )
            alerts = await self.truenas.temperature_alerts(names)
            self.smart.succeed(build_smart_index(temperatures, alerts=alerts))
        except (TrueNASError, OSError) as exc:
            self.smart.fail(str(exc))

    async def poll_system_info(self) -> None:
        if self.truenas is None:
            return
        try:
            self.system_info.succeed(await self.truenas.system_info())
            return
        except (TrueNASError, OSError) as exc:
            first_error = str(exc)
        # system.info accepts only READONLY_ADMIN/SHARING_ADMIN, so on the
        # recommended least-privilege key it is always denied and diagnostics
        # showed truenas_version: null forever. system.version carries no
        # authorization requirement, so the version - the one field the UI
        # actually uses - is recoverable on any key.
        try:
            version = await self.truenas.version()
        except (TrueNASError, OSError):
            self.system_info.fail(first_error)
            return
        if version:
            self.system_info.succeed({"version": version})
        else:
            self.system_info.fail(first_error)

    async def poll_chassis(self) -> None:
        """sg_ses is the expensive source, so it runs on the slowest interval."""
        if not self.ses.available():
            # The honest-absence policy for SAS addresses (see the SesError
            # handler below) must hold on this exit too: sg_ses vanishing
            # mid-run would otherwise leave the last map serving forever.
            self._sas_by_slot.clear()
            self.chassis.fail("sg_ses is not installed in this image")
            return

        loop = asyncio.get_running_loop()
        collected: dict[str, ChassisTelemetry] = dict(self.chassis.value)
        error: str | None = None
        #: Whether this cycle actually obtained a reading from the hardware.
        #: `collected` cannot answer that - it starts as the previous cache
        #: carried forward, so it is non-empty after any past success.
        read_any = False

        for ref in self.enclosures.value:
            if not ref.sg_device:
                # Same honest-absence rule as the SesError paths: an
                # enclosure that lost its sg node cannot refresh its map,
                # so the map must not outlive the ability to rebuild it.
                self._sas_by_slot.pop(ref.logical_id, None)
                continue
            try:
                configuration = await loop.run_in_executor(
                    None, self.ses.read_for, ref, "configuration"
                )
                joined = await loop.run_in_executor(None, self.ses.read_for, ref, "join")
                collected[ref.logical_id] = build_telemetry(
                    ref.logical_id, configuration.stdout, joined.stdout
                )
                read_any = True
            except SesError as exc:
                error = str(exc)
                stale = collected.get(ref.logical_id)
                if stale is not None:
                    stale.stale = True
                    stale.error = error
                # Telemetry is retained and *flagged* stale; the SAS address
                # map cannot be - a bay's sas_address carries no staleness
                # marker - so it is dropped instead. After a drive swap a kept
                # map would show the removed drive's address against the new
                # drive, and no address is strictly better than the wrong
                # drive's.
                self._sas_by_slot.pop(ref.logical_id, None)
                continue

            # The AES page is what maps element index to the sysfs slot number
            # bays() joins on (the same translation the IDENT path performs -
            # see SesLocateWriter). Read in its own try so an AES failure does
            # not mark the just-collected cf/join telemetry stale.
            try:
                aes = await loop.run_in_executor(
                    None, self.ses.read_for, ref, "additional_element_status"
                )
            except SesError as exc:
                self._sas_by_slot.pop(ref.logical_id, None)
                error = str(exc)
                continue
            self._sas_by_slot[ref.logical_id] = _slot_sas_addresses(
                configuration.stdout, aes.stdout
            )

        if error and not collected:
            self.chassis.fail(error)
        elif not read_any:
            # Nothing was read: no enclosure is attached, or none of them still
            # exposes an sg node. succeed() here stamped a cache that had just
            # been handed back its own previous contents, so updated_at jumped
            # to now, due() went false for a full interval, and the diagnostics
            # freshness block reported last_success=<now> / last_error=null for
            # telemetry of unbounded age. That block exists to tell an operator
            # when a source stopped answering (§35); a cycle that read nothing
            # is recorded as the failed attempt it was.
            error = error or "no enclosure with an sg device to read"
            # routes.chassis() serves chassis.value verbatim whenever an entry
            # exists for the requested enclosure, and only falls back to
            # chassis.last_error when there is none - it never separately checks
            # the cache-level failure this branch just recorded. Without marking
            # the RETAINED objects themselves, a shelf that stops exposing an sg
            # node would keep serving fan speeds and temperatures of unbounded
            # age with stale=false on the one page an operator actually looks
            # at, while only the diagnostics block knew. Everything still in
            # `collected` is carried-forward cache rather than a reading from
            # this cycle, so it all gets the treatment the SesError branch above
            # gives a single enclosure.
            for retained in collected.values():
                retained.stale = True
                retained.error = error
            self.chassis.fail(error)
            # And the same honest-absence rule the SesError paths apply: with
            # no enclosure contributing a reading, no address map can be
            # rebuilt, and a kept one would show a removed drive's address.
            self._sas_by_slot.clear()
        else:
            self.chassis.succeed(collected)
            if error:
                self.chassis.last_error = error

    async def _notify_health_changes(self) -> None:
        """Announce bay health transitions. Never allowed to break polling."""
        if self.notifier is None or not self.notifier.enabled:
            return
        loop = asyncio.get_running_loop()
        try:
            for ref in self.enclosures.value:
                # _compose_bays() is synchronous sysfs I/O (~7 identity
                # attributes per disk on a cache miss), so it runs in the
                # executor like every other blocking read reached from the poll
                # loop.
                composed = await loop.run_in_executor(
                    None, self._compose_bays, ref.logical_id
                )
                await self.notifier.evaluate(
                    [bay for bay, _identified in composed if _notifiable(bay)]
                )
        except Exception:  # noqa: BLE001 - notification is best-effort
            log.exception("health notification failed")

    async def _loop(self) -> None:
        while True:
            try:
                polled = False
                # Gated on the slot cache alone, because poll_hardware() stamps
                # it on every path it can take - success, a read_slots()
                # failure, a discover() failure - so it is the one clock that
                # always advances. The gate used to be
                # `enclosures.due(...) or not self.slots.value`, and both halves
                # failed open together on an unplugged shelf: discover() raising
                # left the enclosure clock frozen AND the slot cache empty, so
                # the emptiness test re-entered this poll every single tick,
                # taking the cross-process enclosure flock each time (§37 says
                # degrade the section, not hammer the hardware). Freshness on
                # the failure path is what makes a retry interval mean anything.
                if self.slots.due(self.settings.poll_slots_seconds):
                    await self.poll_hardware()
                    polled = True
                if self.zfs.due(self.settings.poll_truenas_seconds):
                    await self.poll_truenas()
                    polled = True
                if self.smart.due(self.settings.poll_smart_seconds):
                    await self.poll_smart()
                    polled = True
                if self.system_info.due(300):
                    await self.poll_system_info()
                if self.chassis.due(self.settings.poll_ses_seconds):
                    await self.poll_chassis()
                    polled = True
                # Only when something actually changed. This used to run every
                # loop tick - once a second - and evaluate() composes every bay,
                # which reads ~7 sysfs attributes per disk. On a 15-bay shelf
                # that was ~100 file reads a second, forever, purely to
                # re-answer a question whose inputs had not moved.
                if polled:
                    await self._notify_health_changes()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - polling must never die
                log.exception("unexpected error in poll loop")
            await asyncio.sleep(1.0)

    async def start(self) -> None:
        await self.poll_hardware()
        observed = {
            (ref.logical_id, slot.ses_slot): slot.locate
            for ref in self.enclosures.value
            for slot in self.slots.value.get(ref.logical_id, [])
        }
        await self.ident.reconcile(observed)
        self.ident.start()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        await self.ident.stop()
        if self.truenas is not None:
            # The client now holds a long-lived socket; close it so shutdown
            # does not leave a connection open on the appliance.
            await self.truenas.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------ composition

    def enclosure(self, logical_id: str) -> EnclosureRef:
        for ref in self.enclosures.value:
            if ref.logical_id == logical_id.lower():
                return ref
        raise EnclosureNotFoundError(f"enclosure {logical_id} is not attached")

    def _sas_addresses(self, logical_id: str) -> dict[int, str]:
        """SES-reported SAS port address per sysfs slot number.

        Deliberately not used to correlate slots to disks - it is the port
        address and differs from the block layer's node WWN - but it is worth
        displaying, and it is the only place the drive's SAS identity appears.

        Keyed by the AES page's device slot number because that is the
        coordinate bays() joins on: the kernel fills the sysfs ``slot``
        attribute from that field. This map used to be keyed by element index
        instead - the same numbering conflation the IDENT path had to fix (see
        SesLocateWriter): the two coincide on the KTN-STL3 but SES-3 does not
        guarantee it, and on a permuted shelf that displayed the wrong drive's
        address. When the AES page is unavailable the map is empty and no
        address is shown - honest absence beats a wrong join.
        """
        return self._sas_by_slot.get(logical_id, {})

    def bays(self, logical_id: str) -> list[Bay]:
        return [bay for bay, _ in self._compose_bays(logical_id)]

    def _compose_bays(self, logical_id: str) -> list[tuple[Bay, bool]]:
        """Compose each bay, paired with whether its disk identity is settled.

        The flag says whether *this composition* could establish which disk
        occupies the bay - false only while a live sysfs identity read fails.
        It is deliberately not a field on Bay: it describes the state of our
        knowledge for one cycle, not a property of the hardware, and the API
        surface must not grow a field that means "distrust the neighbouring
        fields". The one caller that needs it is the notifier (_notifiable),
        because it is the one consumer that persists a verdict and wakes a
        phone rather than redrawing on the next poll.
        """
        ref = self.enclosure(logical_id)
        composed: list[tuple[Bay, bool]] = []
        sas_addresses = self._sas_addresses(ref.logical_id)

        for slot in self.slots.value.get(ref.logical_id, []):
            device = slot.block_device
            try:
                local = self.disk_reader.read(device)
                identified = True
            except DiskIdentityUnreadable as exc:
                # The block node is there and will not say what it is: a SCSI
                # re-probe in flight, or a disk failing hard enough that its
                # own attributes EIO. Before this was distinguishable, the read
                # came back as an *empty* identity, which is the one value
                # _identity_conflict must answer False to (§20 - absence of
                # data is not data), so the guard below switched itself off at
                # the exact moment it was needed. §20 cuts both ways: a read
                # that FAILED is no more evidence of agreement than it is of
                # conflict, and the honest composition treats the bay as one
                # whose occupant is unknown for this cycle.
                log.debug("identity unreadable for %s: %s", device, exc)
                local = DiskIdentity()
                identified = False

            remote = self.remote_disks.value.get(device or "")
            zfs = self.zfs.value.get(device or "", ZfsInfo())
            smart = self.smart.value.get(device or "", SmartInfo())
            # The TrueNAS caches are keyed by transient block name, so after a
            # swap that reuses the name the removed disk's record can wear the
            # new disk's name for up to one poll_truenas interval. Attaching
            # any of it requires a live read that positively places this disk
            # in this bay: identifiers that disagree prove the record belongs
            # to the removed disk, and identifiers that could not be read prove
            # nothing at all. Neither may be joined, so the remote record, the
            # ZFS state and the SMART data indexed by that name are dropped
            # together - health included, which is what stopped a re-probing
            # bay from being rendered FAILED with a departed drive's pool
            # membership and 71C alert (§20).
            # Two very different situations, and collapsing them silences the
            # shelf. A live read that DISAGREES proves the record describes a
            # drive that has left; dropping it is the whole point. A live read
            # that FAILED proves nothing either way - and treating "unknown
            # occupant" as "no failure to report" is the more dangerous
            # mistake of the two. It made a bay reading zfs FAULTED and SMART
            # 71C render OK and go unnotified, and because an EACCES on the
            # container's disk access takes exactly that shape shelf-wide, it
            # could turn 15 failing bays into 15 healthy-looking ones.
            #
            # So a monitoring tool fails loud: a proven swap drops the record,
            # while an unconfirmed occupant keeps every failure signal and
            # withholds only the IDENTITY it cannot vouch for. A false alarm
            # naming no drive is recoverable; silence on a dying one is not.
            swapped = remote is not None and _identity_conflict(local, remote)
            if swapped:
                remote = None
                zfs = ZfsInfo()
                smart = SmartInfo()
            # merge_identity is what backfills serial/model/WWN from the
            # name-keyed TrueNAS record, so it is gated on a live read that
            # positively places this disk here. zfs and smart deliberately are
            # not: they describe the BAY's condition, and an alert that names
            # no drive still tells an operator which bay to look at.
            identity = merge_identity(local, remote if identified else None)
            joinable = identified and not swapped
            if joinable:
                # The SAS address is slot-keyed rather than name-keyed, but it
                # is still up to poll_ses_seconds old and still describes
                # whichever drive was in the bay when the AES page was read. It
                # is also the field an operator uses to cross-check a bay
                # against the physical shelf, so serving the previous drive's
                # port address at the moment we have PROVEN the occupant
                # changed is the worst possible time to be one poll behind. It
                # rides the same gate as the rest: no address beats the wrong
                # drive's address, the identical refusal poll_chassis applies
                # when it drops the map instead of keeping a stale one.
                identity.sas_address = sas_addresses.get(slot.ses_slot)
            # The IDENT manager gets the age of this reading, not just its
            # value, because it holds the newer evidence: a write it verified
            # by hardware read-back after this snapshot was taken supersedes
            # what the snapshot says (§27, IdentManager.describe). Its verdict
            # replaces slot.locate for the whole bay rather than only feeding
            # origin - a bay reported lit with no origin, or dark with a live
            # countdown, would just be a differently-shaped lie.
            locate, origin, expires = self.ident.describe(
                ref.logical_id, slot.ses_slot, slot.locate, self.slots.observed_monotonic
            )

            composed.append((
                Bay(
                    display_bay=slot.display_bay,
                    ses_slot=slot.ses_slot,
                    enclosure_id=ref.logical_id,
                    device=f"/dev/{device}" if device else None,
                    health=classify(slot.status, slot.fault, bool(device), zfs, smart),
                    status=slot.status,
                    power_status=slot.power_status,
                    locate=locate,
                    fault=slot.fault,
                    ident_expires_at=expires,
                    ident_origin=origin,
                    disk=identity,
                    zfs=zfs,
                    smart=smart,
                    sysfs_path=slot.sysfs_path,
                ),
                identified,
            ))
        return composed

    def diagnostics(self) -> dict[str, Any]:
        """Copyable, sanitised diagnostics (§35). Contains no secrets."""
        refs = self.enclosures.value
        return {
            "app_version": __version__,
            "started_at": self._started_at.isoformat(),
            "truenas_version": self.system_info.value.get("version"),
            # Null here is not necessarily a fault. `system.info` is the one
            # call this app makes that no narrow role satisfies - it needs
            # READONLY_ADMIN or SHARING_ADMIN - so a least-privilege API key
            # legitimately cannot fetch it, and everything else still works.
            # Surfacing the reason stops that reading as a broken connection.
            "truenas_version_unavailable_reason": (
                None if self.system_info.value.get("version")
                else self.system_info.last_error
            ),
            "truenas_configured": self.truenas is not None,
            "truenas_url": self.settings.truenas_url or None,
            "truenas_tls_verified": self.settings.truenas_verify_tls,
            "sg_ses_binary": self.ses.binary,
            "sg_ses_version": self.ses.version(),
            "sg_ses_available": self.ses.available(),
            "sysfs_root": str(self.settings.sysfs_root),
            "ident_helper_socket": str(self.settings.ident_helper_socket)
            if self.settings.ident_helper_socket
            else None,
            "enclosures": [
                {
                    "logical_id": r.logical_id,
                    "vendor": r.vendor,
                    "product": r.product,
                    "revision": r.revision,
                    "scsi_address": r.scsi_address,
                    "sysfs_path": r.sysfs_path,
                    "sg_device": r.sg_device,
                    "bsg_device": r.bsg_device,
                    "slot_count": r.slot_count,
                    "slots_discovered": len(self.slots.value.get(r.logical_id, [])),
                }
                for r in refs
            ],
            "polling": {
                # discover() failing (an unplugged shelf, a lost cross-process
                # lock) previously left no trace on any surface: poll_hardware
                # records it with enclosures.fail(), but nothing read it back,
                # so the one signal distinguishing "never polled" from "polling
                # and failing" existed only in memory (§35). Same interval as
                # slots: one poll_hardware() call stamps both.
                "enclosures": _freshness(self.enclosures, self.settings.poll_slots_seconds),
                "slots": _freshness(self.slots, self.settings.poll_slots_seconds),
                "truenas": _freshness(self.zfs, self.settings.poll_truenas_seconds),
                "smart": _freshness(self.smart, self.settings.poll_smart_seconds),
                "chassis": _freshness(self.chassis, self.settings.poll_ses_seconds),
                # Included so a denied system.info is visible rather than only
                # showing up as a missing version string.
                "system_info": _freshness(self.system_info, 300),
            },
        }


def _slot_sas_addresses(configuration_text: str, aes_text: str) -> dict[int, str]:
    """Map AES device slot number -> the drive's own SAS address.

    Blocks are filtered to the bay type descriptor discovered from the
    configuration page, mirroring SesLocateWriter._slot_element_map: on a
    shelf exposing both bay element types, only the descriptor the kernel
    built the sysfs slots from may contribute. A slot number two elements both
    claim is unaddressable, so it yields no address at all rather than an
    arbitrary pick - the same refusal policy the IDENT path applies, because a
    wrong address misidentifies a physical drive to the operator.
    """
    type_index = array_slot_type_index(parse_configuration(configuration_text)[1])
    if type_index is None:
        return {}
    blocks = parse_additional_element_status(aes_text)
    # A device slot number under a non-bay block is proof sg_ses printed a
    # bay descriptor under the wrong header (an omitted optional AES block
    # shifts its positional print loop); every attribution on such a page is
    # suspect, so no addresses are served from it - the same page-level
    # refusal the IDENT path applies. Slot and address ride the same
    # descriptor, so this is the only positional failure that can reach the
    # display join.
    for block in blocks:
        if block.element_type in BAY_ELEMENT_TYPES:
            continue
        if any("device_slot_number" in entry for entry in block.entries):
            return {}
    first_chosen_seen = False
    claimed: dict[int, tuple[bool, str | None] | None] = {}
    for block in blocks:
        chosen = block.type_index == type_index and not first_chosen_seen
        if block.type_index == type_index:
            first_chosen_seen = True
        for entry in block.entries:
            slot_number = entry.get("device_slot_number")
            # An empty bay carries no protocol descriptor and thus no slot
            # number; it simply stays unmapped. An entry WITH a slot number
            # but no parsed address still claims the slot: a claim poisons
            # duplicates whether or not its own address was printed
            # (a SATA drive behind a protocol we do not parse must still
            # invalidate a colliding claim, or the refusal policy has a
            # hole exactly where firmware is at its weirdest).
            if not isinstance(slot_number, int):
                continue
            address = entry.get("sas_address")
            if slot_number in claimed:
                claimed[slot_number] = None
            else:
                claimed[slot_number] = (
                    chosen,
                    address if isinstance(address, str) else None,
                )
    return {
        slot: claim[1]
        for slot, claim in claimed.items()
        if claim is not None and claim[0] and claim[1] is not None
    }


def _freshness(cache: Cached[Any], interval: float) -> dict[str, Any]:
    return {
        "interval_seconds": interval,
        "last_success": cache.updated_at.isoformat() if cache.updated_at else None,
        "last_attempt": cache.last_attempt_at.isoformat() if cache.last_attempt_at else None,
        "last_error": cache.last_error,
    }
