"""Generic SES enclosure discovery and slot control over the Linux enclosure sysfs ABI.

This is an independent implementation of the *concepts* validated in TrueNAS'
``middlewared.plugins.enclosure_.sysfs_disks`` (``map_disks_to_enclosure_slots``
and ``toggle_enclosure_slot_identifier``). No TrueNAS private module is imported
and no TrueNAS file is patched: the only dependency is the stable Linux
enclosure sysfs ABI, so this survives TrueNAS upgrades (spec §17, §48).

Everything is rooted at an injectable ``sysfs_root`` so the whole layer is
testable against captured fixtures with no hardware present (§42).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ktnmgr.enclosure.access import enclosure_access
from ktnmgr.models import EnclosureRef, SlotState

log = logging.getLogger(__name__)

DEFAULT_SYSFS_ROOT = Path("/sys")
DEFAULT_DEV_ROOT = Path("/dev")

# How long to wait for a locate write to be reflected back by sysfs, and how
# often to re-read while waiting. Measured settle time on the KTN-STL3 is
# 0.17-0.22s; 2s leaves generous headroom for a busy expander without making a
# genuinely failed write hang the request.
DEFAULT_SETTLE_TIMEOUT = 2.0
DEFAULT_SETTLE_POLL = 0.05

# A slot directory is any child of the enclosure directory that carries a
# 'slot' attribute. Non-slot children (device, power, subsystem, components,
# id, uevent) are skipped by that test rather than by name, so the code does
# not depend on this vendor's particular directory naming.
_SLOT_ATTR = "slot"

_SCSI_ADDR_RE = re.compile(r"^\d+:\d+:\d+:\d+$")


def _read_text(path: Path) -> str | None:
    """Read a sysfs attribute, returning None if absent or unreadable.

    sysfs reads can fail with EIO/ENODEV if the device vanishes mid-scan
    (§37: enclosure disconnected, HBA reset). That is normal, not exceptional.
    """
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_bool(path: Path) -> bool:
    value = _read_text(path)
    return value is not None and value.strip() not in ("0", "", "off")


class EnclosureNotFoundError(LookupError):
    """Raised when a previously known enclosure is no longer attached."""


class SlotNotFoundError(LookupError):
    """Raised when a slot index does not exist on the enclosure."""


class SysfsEnclosureBackend:
    """Reads (and, for locate only, writes) the Linux enclosure sysfs tree."""

    def __init__(
        self,
        sysfs_root: Path = DEFAULT_SYSFS_ROOT,
        dev_root: Path = DEFAULT_DEV_ROOT,
        lock_path: Path | str | None = None,
    ) -> None:
        self.sysfs_root = Path(sysfs_root)
        self.dev_root = Path(dev_root)
        # Reading a slot attribute is not a passive file read: it makes the
        # kernel ses driver issue a diagnostic to the shelf. That is why these
        # reads take the same lock an IDENT write does - so a sweep cannot
        # sample a bay between a write and its settle read-back. See
        # enclosure/access.py.
        self.lock_path = lock_path

    # ------------------------------------------------------------------
    # Discovery (§18)
    # ------------------------------------------------------------------

    @property
    def _class_dir(self) -> Path:
        return self.sysfs_root / "class" / "enclosure"

    def discover(self) -> list[EnclosureRef]:
        """Enumerate every SES enclosure the kernel has bound.

        Paths such as ``1:0:15:0`` and ``/dev/sg16`` are discovered, never
        assumed (§18). Results are sorted by logical id so ordering is stable
        across reboots.
        """
        if not self._class_dir.is_dir():
            log.warning("no %s - kernel enclosure support absent or not mounted", self._class_dir)
            return []

        # Locked so a discovery cannot land between an IDENT write and its
        # settle read-back. This runs far more often than it looks: resolve()
        # calls it, so the helper does a full discovery before every sg_ses
        # read.
        with enclosure_access(self.lock_path):
            found: list[EnclosureRef] = []
            for entry in sorted(self._class_dir.iterdir()):
                ref = self._describe(entry)
                if ref is not None:
                    found.append(ref)
        found.sort(key=lambda e: e.logical_id)
        return found

    def _describe(self, path: Path) -> EnclosureRef | None:
        real = path.resolve() if path.is_symlink() else path
        if not real.is_dir():
            return None

        device = real / "device"
        vendor = _read_text(device / "vendor") or ""
        product = _read_text(device / "model") or ""
        revision = _read_text(device / "rev") or ""

        # The enclosure logical identifier is exposed directly by the kernel,
        # so persistent identity needs no sg_ses round trip at all.
        logical_id = _read_text(real / "id") or ""
        if not logical_id:
            log.debug("skipping %s: no logical id attribute", real)
            return None

        scsi_address = path.name if _SCSI_ADDR_RE.match(path.name) else real.name

        return EnclosureRef(
            logical_id=logical_id.strip().lower(),
            vendor=vendor.strip(),
            product=product.strip(),
            revision=revision.strip(),
            scsi_address=scsi_address,
            sysfs_path=str(real),
            sg_device=self._sg_device(device),
            bsg_device=self._bsg_device(scsi_address),
            slot_count=len(self._slot_dirs(real)),
        )

    def _sg_device(self, device_dir: Path) -> str | None:
        sg_dir = device_dir / "scsi_generic"
        if not sg_dir.is_dir():
            return None
        for child in sorted(sg_dir.iterdir()):
            return str(self.dev_root / child.name)
        return None

    def _bsg_device(self, scsi_address: str) -> str | None:
        candidate = self.dev_root / "bsg" / scsi_address
        return str(candidate) if candidate.exists() else None

    # ------------------------------------------------------------------
    # Slot enumeration (§6, §18)
    # ------------------------------------------------------------------

    def _slot_dirs(self, enclosure_path: Path) -> list[Path]:
        if not enclosure_path.is_dir():
            return []
        return [c for c in enclosure_path.iterdir() if c.is_dir() and (c / _SLOT_ATTR).exists()]

    def resolve(self, logical_id: str) -> EnclosureRef:
        """Re-resolve an enclosure by persistent identity.

        Called before every operation so a changed /dev/sgX or sysfs path is
        picked up rather than cached into a stale write (§37).
        """
        target = logical_id.strip().lower()
        for ref in self.discover():
            if ref.logical_id == target:
                return ref
        raise EnclosureNotFoundError(f"enclosure {logical_id} is not attached")

    def read_slots(self, ref: EnclosureRef) -> list[SlotState]:
        """Read every bay's state.

        The ``slot`` attribute is authoritative for the slot number; the
        directory name is only a fallback. On this hardware they agree, but
        the ABI does not guarantee it.
        """
        enclosure_path = Path(ref.sysfs_path)
        states: list[SlotState] = []

        # One lock for the whole sweep, not one per slot, so the map the UI
        # renders is a single consistent picture of the shelf rather than 15
        # bays sampled at 15 different moments.
        with enclosure_access(self.lock_path):
            for slot_dir in self._slot_dirs(enclosure_path):
                raw_slot = _read_text(slot_dir / _SLOT_ATTR)
                try:
                    ses_slot = int(raw_slot) if raw_slot is not None else int(slot_dir.name)
                except ValueError:
                    log.warning("slot %s has unparseable slot attribute %r", slot_dir, raw_slot)
                    continue

                states.append(
                    SlotState(
                        ses_slot=ses_slot,
                        display_bay=ses_slot + 1,
                        status=_read_text(slot_dir / "status") or "unknown",
                        power_status=_read_text(slot_dir / "power_status"),
                        locate=_read_bool(slot_dir / "locate"),
                        fault=_read_bool(slot_dir / "fault"),
                        active=_read_bool(slot_dir / "active")
                        if (slot_dir / "active").exists()
                        else None,
                        block_device=self._block_device(slot_dir),
                        sysfs_path=str(slot_dir),
                    )
                )

        states.sort(key=lambda s: s.ses_slot)
        return states

    def _block_device(self, slot_dir: Path) -> str | None:
        """Map a bay to its block device.

        This is the authoritative slot-to-disk mapping and the reason the app
        does not attempt to correlate by SAS address: SES reports the SAS
        *port* address while the block layer reports the node WWN, and on this
        hardware they differ by 2.
        """
        block_dir = slot_dir / "device" / "block"
        if not block_dir.is_dir():
            return None
        for child in sorted(block_dir.iterdir()):
            return child.name
        return None

    def slot_dir(self, ref: EnclosureRef, ses_slot: int) -> Path:
        for slot_dir in self._slot_dirs(Path(ref.sysfs_path)):
            raw = _read_text(slot_dir / _SLOT_ATTR)
            try:
                if raw is not None and int(raw) == ses_slot:
                    return slot_dir
            except ValueError:
                continue
        raise SlotNotFoundError(f"slot {ses_slot} not present on enclosure {ref.logical_id}")

    # ------------------------------------------------------------------
    # IDENT - the only write this application performs (§9, §15)
    # ------------------------------------------------------------------

    def read_locate(self, ref: EnclosureRef, ses_slot: int) -> bool:
        with enclosure_access(self.lock_path):
            return _read_bool(self.slot_dir(ref, ses_slot) / "locate")

    def read_locate_at(self, path: Path) -> bool:
        """Indirection point so tests can simulate a slow-settling attribute."""
        return _read_bool(path)

    def set_locate(
        self,
        ref: EnclosureRef,
        ses_slot: int,
        on: bool,
        settle_timeout: float = DEFAULT_SETTLE_TIMEOUT,
        poll_interval: float = DEFAULT_SETTLE_POLL,
    ) -> bool:
        """Write locate and verify by reading it back (§26 steps 6-9).

        The read-back POLLS rather than reading once. On the KTN-STL3 the
        sysfs attribute does not update synchronously with the write: the
        kernel dispatches a SES control command and refreshes the cached value
        only once the enclosure processor has answered. Measured on real
        hardware this takes 0.17-0.22s, so a single immediate read returns the
        *previous* value and every operation would be reported as a failed
        verification while actually having succeeded.

        Returns the settled state, or the last value observed if it never
        settled within ``settle_timeout`` - which the caller then reports as a
        genuine verification failure.
        """
        # The write and its settle poll are one atomic operation. The
        # attribute does not update until the enclosure processor answers, so
        # without the lock a concurrent slot sweep can read the bay mid-flight
        # and cache a half-applied locate state for the UI to render.
        with enclosure_access(self.lock_path):
            target = self.slot_dir(ref, ses_slot) / "locate"
            payload = "1" if on else "0"
            with target.open("w") as handle:
                handle.write(payload)

            deadline = time.monotonic() + settle_timeout
            observed = self.read_locate_at(target)
            while observed is not on and time.monotonic() < deadline:
                time.sleep(poll_interval)
                observed = self.read_locate_at(target)

        if observed is not on:
            log.warning(
                "locate on %s slot %s did not settle to %s within %ss (still %s)",
                ref.logical_id, ses_slot, int(on), settle_timeout, int(observed),
            )
        return observed
