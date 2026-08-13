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
        """Normalise sysfs wwid ('naa.5000cca058347d84') to '0x5000cca058347d84'.

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
        """Return identity for a block device name, or an empty identity."""
        if not name:
            return DiskIdentity()

        block_dir = self._block_dir(name)
        if not block_dir.is_dir():
            log.debug("block device %s not present under %s", name, self.sysfs_root)
            return DiskIdentity()

        device_dir = block_dir / "device"

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

        return DiskIdentity(
            serial=self._serial(device_dir),
            wwn=self._normalise_wwn(self._text(device_dir / "wwid")),
            model=model,
            firmware=self._text(device_dir / "rev"),
            size_bytes=size_bytes,
            rotational=rotational,
        )
