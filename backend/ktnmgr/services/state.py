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
from ktnmgr.enclosure.disks import DiskInfoReader
from ktnmgr.enclosure.ses import SesError, SesRunner
from ktnmgr.enclosure.ses_parser import build_telemetry
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


@dataclass
class Cached(Generic[T]):
    """Last-good value plus the freshness metadata the diagnostics page shows."""

    value: T
    updated_at: datetime | None = None
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    _monotonic: float = field(default=0.0, repr=False)

    def due(self, interval: float) -> bool:
        return (time.monotonic() - self._monotonic) >= interval

    def succeed(self, value: T) -> None:
        self.value = value
        self.updated_at = datetime.now(UTC)
        self.last_attempt_at = self.updated_at
        self.last_error = None
        self._monotonic = time.monotonic()

    def fail(self, error: str) -> None:
        self.last_attempt_at = datetime.now(UTC)
        self.last_error = error
        self._monotonic = time.monotonic()


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
    if not has_device:
        return SlotHealth.EMPTY
    if fault or normalised in ("critical", "unrecoverable"):
        return SlotHealth.FAILED
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

        self._task: asyncio.Task[None] | None = None
        self._started_at = datetime.now(UTC)

    # --------------------------------------------------------------- polling

    async def poll_hardware(self) -> None:
        """sysfs: cheap, frequent, and the only source the bay map truly needs."""
        try:
            found = [e for e in self.backend.discover() if self._allowed(e.logical_id)]
            self.enclosures.succeed(found)
            slots = {ref.logical_id: self.backend.read_slots(ref) for ref in found}
            self.slots.succeed(slots)
        except OSError as exc:
            self.slots.fail(str(exc))

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
            self.chassis.fail("sg_ses is not installed in this image")
            return

        loop = asyncio.get_running_loop()
        collected: dict[str, ChassisTelemetry] = dict(self.chassis.value)
        error: str | None = None

        for ref in self.enclosures.value:
            if not ref.sg_device:
                continue
            try:
                configuration = await loop.run_in_executor(
                    None, self.ses.read_for, ref, "configuration"
                )
                joined = await loop.run_in_executor(None, self.ses.read_for, ref, "join")
                collected[ref.logical_id] = build_telemetry(
                    ref.logical_id, configuration.stdout, joined.stdout
                )
            except SesError as exc:
                error = str(exc)
                stale = collected.get(ref.logical_id)
                if stale is not None:
                    stale.stale = True
                    stale.error = error

        if error and not collected:
            self.chassis.fail(error)
        else:
            self.chassis.succeed(collected)
            if error:
                self.chassis.last_error = error

    async def _notify_health_changes(self) -> None:
        """Announce bay health transitions. Never allowed to break polling."""
        if self.notifier is None or not self.notifier.enabled:
            return
        try:
            for ref in self.enclosures.value:
                await self.notifier.evaluate(self.bays(ref.logical_id))
        except Exception:  # noqa: BLE001 - notification is best-effort
            log.exception("health notification failed")

    async def _loop(self) -> None:
        while True:
            try:
                polled = False
                if self.enclosures.due(self.settings.poll_slots_seconds) or not self.slots.value:
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
        """SES-reported SAS port address per slot, from cached telemetry.

        Deliberately not used to correlate slots to disks - it is the port
        address and differs from the block layer's node WWN - but it is worth
        displaying, and it is the only place the drive's SAS identity appears.
        """
        telemetry = self.chassis.value.get(logical_id)
        if telemetry is None:
            return {}
        return {
            element.element_index: element.fields["SAS address"]
            for element in telemetry.elements
            if element.element_type == "Array device slot"
            and not element.is_overall
            and "SAS address" in element.fields
        }

    def bays(self, logical_id: str) -> list[Bay]:
        ref = self.enclosure(logical_id)
        composed: list[Bay] = []
        sas_addresses = self._sas_addresses(ref.logical_id)

        for slot in self.slots.value.get(ref.logical_id, []):
            device = slot.block_device
            local = self.disk_reader.read(device)
            identity = merge_identity(local, self.remote_disks.value.get(device or ""))
            identity.sas_address = sas_addresses.get(slot.ses_slot)
            zfs = self.zfs.value.get(device or "", ZfsInfo())
            smart = self.smart.value.get(device or "", SmartInfo())
            origin, expires = self.ident.describe(ref.logical_id, slot.ses_slot, slot.locate)

            composed.append(
                Bay(
                    display_bay=slot.display_bay,
                    ses_slot=slot.ses_slot,
                    enclosure_id=ref.logical_id,
                    device=f"/dev/{device}" if device else None,
                    health=classify(slot.status, slot.fault, bool(device), zfs, smart),
                    status=slot.status,
                    power_status=slot.power_status,
                    locate=slot.locate,
                    fault=slot.fault,
                    ident_expires_at=expires,
                    ident_origin=origin,
                    disk=identity,
                    zfs=zfs,
                    smart=smart,
                    sysfs_path=slot.sysfs_path,
                )
            )
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
                "slots": _freshness(self.slots, self.settings.poll_slots_seconds),
                "truenas": _freshness(self.zfs, self.settings.poll_truenas_seconds),
                "smart": _freshness(self.smart, self.settings.poll_smart_seconds),
                "chassis": _freshness(self.chassis, self.settings.poll_ses_seconds),
                # Included so a denied system.info is visible rather than only
                # showing up as a missing version string.
                "system_info": _freshness(self.system_info, 300),
            },
        }


def _freshness(cache: Cached[Any], interval: float) -> dict[str, Any]:
    return {
        "interval_seconds": interval,
        "last_success": cache.updated_at.isoformat() if cache.updated_at else None,
        "last_attempt": cache.last_attempt_at.isoformat() if cache.last_attempt_at else None,
        "last_error": cache.last_error,
    }
