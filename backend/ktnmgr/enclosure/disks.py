"""Local block-device identity, read straight from sysfs.

This exists so a bay still shows its serial, model, firmware and capacity when
the TrueNAS API is unreachable (spec §37). TrueNAS remains the source of truth
for pool/vdev/ZFS state, which sysfs cannot know.

No shelling out: every value here comes from a sysfs read.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ktnmgr.models import DiskIdentity

log = logging.getLogger(__name__)

# /sys/block/<dev>/size is always expressed in 512-byte sectors regardless of
# the device's logical block size.
_SECTOR_BYTES = 512

# VPD page 0x80 (unit serial number) layout: 4-byte header then the serial.
_VPD_HEADER_BYTES = 4


class DiskInfoReader:
    """Reads stable disk identity attributes for a block device name."""

    def __init__(self, sysfs_root: Path = Path("/sys")) -> None:
        self.sysfs_root = Path(sysfs_root)
        # name -> (wwid seen when cached, identity). See read() for why the
        # wwid is part of the key rather than the name alone.
        self._cache: dict[str, tuple[str | None, DiskIdentity]] = {}

    def _block_dir(self, name: str) -> Path:
        return self.sysfs_root / "block" / name

    @staticmethod
    def _text(path: Path) -> str | None:
        try:
            value = path.read_text(errors="replace").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return value or None

    def _serial(self, device_dir: Path) -> str | None:
        """Prefer the SCSI unit-serial VPD page; fall back to a 'serial' attr."""
        vpd = device_dir / "vpd_pg80"
        try:
            raw = vpd.read_bytes()
        except OSError:
            raw = b""
        if len(raw) > _VPD_HEADER_BYTES:
            candidate = raw[_VPD_HEADER_BYTES:].decode("ascii", errors="ignore")
            candidate = "".join(c for c in candidate if c.isprintable()).strip()
            if candidate:
                return candidate
        return self._text(device_dir / "serial")

    @staticmethod
    def _normalise_wwn(raw: str | None) -> str | None:
        """Normalise sysfs wwid ('naa.5000cca0e0000000') to '0x5000cca0e0000000'.

        This is the block layer's node WWN. It is deliberately NOT compared
        against the SAS address reported by SES, which is a port address and
        differs on this hardware.
        """
        if not raw:
            return None
        value = raw.strip().split()[0]
        for prefix in ("naa.", "0x", "eui.", "wwn-"):
            if value.lower().startswith(prefix):
                value = value[len(prefix) :]
                break
        value = value.strip().lower()
        return f"0x{value}" if value else None

    def read(self, name: str | None) -> DiskIdentity:
        """Return identity for a block device name, or an empty identity.

        Cached, because none of these attributes change while a disk sits in a
        bay, and composing a 15-bay map re-read all of them for every caller.

        The cache is keyed on ``(name, wwid)``, never on the name alone.
        ``/dev/sdX`` is not identity and is provably reused: on the validation
        system a replacement drive was assigned the same ``sdf`` the removed
        drive had held. A name-keyed cache would then have shown the previous
        drive's serial against the new disk - the exact confusion this
        application exists to prevent. Re-reading the one wwid attribute to
        confirm the disk is still the same one costs a single file read and
        saves the other six.
        """
        if not name:
            return DiskIdentity()

        block_dir = self._block_dir(name)
        if not block_dir.is_dir():
            log.debug("block device %s not present under %s", name, self.sysfs_root)
            self._cache.pop(name, None)
            return DiskIdentity()

        device_dir = block_dir / "device"

        raw_wwid = self._text(device_dir / "wwid")
        cached = self._cache.get(name)
        if cached is not None and raw_wwid is not None and cached[0] == raw_wwid:
            return cached[1]

        size_bytes: int | None = None
        raw_size = self._text(block_dir / "size")
        if raw_size and raw_size.isdigit():
            size_bytes = int(raw_size) * _SECTOR_BYTES

        rotational: bool | None = None
        raw_rota = self._text(block_dir / "queue" / "rotational")
        if raw_rota in ("0", "1"):
            rotational = raw_rota == "1"

        vendor = self._text(device_dir / "vendor")
        model = self._text(device_dir / "model")
        if vendor and model and not model.startswith(vendor):
            model = f"{vendor} {model}".strip()

        identity = DiskIdentity(
            serial=self._serial(device_dir),
            wwn=self._normalise_wwn(raw_wwid),
            model=model,
            firmware=self._text(device_dir / "rev"),
            size_bytes=size_bytes,
            rotational=rotational,
        )
        # Only cacheable when the disk offers a wwid to validate against.
        # Without one there is no cheap way to tell a re-used device name from
        # the same disk, so it is re-read every time rather than risk showing
        # one drive's identity for another.
        if raw_wwid is not None:
            self._cache[name] = (raw_wwid, identity)
        else:
            self._cache.pop(name, None)
        return identity
