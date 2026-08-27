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


class DiskIdentityUnreadable(OSError):
    """The device is present and would not say which disk it is.

    Deliberately distinct from an absent device, which still yields an empty
    identity. Both come back with no serial and no WWN, but they are opposite
    kinds of statement: "this disk exposes no identifiers" is a fact about the
    disk, while "the identifier reads failed" is a gap in our own knowledge.
    Spec §20 forbids treating the first as data - and the same rule forbids
    treating the second as agreement, which is what a silently-empty identity
    became at every caller that compares it against another source
    (StateService._compose_bays).

    The window is real and is exactly when it hurts: during a SCSI re-probe,
    or on a disk failing hard enough that its sysfs attributes return EIO, the
    block node still exists while nothing can be read through it.

    An OSError subclass because that is what it is, and because a caller that
    already degrades on I/O failure keeps degrading correctly without knowing
    this type exists.
    """


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
    def _text_or_failure(path: Path) -> tuple[str | None, bool]:
        """Attribute text, plus whether the read FAILED rather than the
        attribute being absent.

        ENOENT is not a failure: a driver that does not expose ``wwid`` or
        ``serial`` simply has nothing to say, and the tree under a live disk is
        full of attributes that legitimately do not exist. Any other OSError -
        EIO from a disk that stopped answering, ENODEV mid-re-probe, EACCES
        under a tighter container profile - means the attribute is there and we
        could not read it. Only the second may be reported as unknown identity;
        collapsing the two is what let an unreadable disk masquerade as a disk
        with nothing to declare (see DiskIdentityUnreadable).

        A decode error is not a failure either: the bytes arrived, they were
        just unusable, which is again a fact about the disk.
        """
        try:
            value = path.read_text(errors="replace").strip()
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, True
        except UnicodeDecodeError:
            return None, False
        return (value or None), False

    @classmethod
    def _text(cls, path: Path) -> str | None:
        return cls._text_or_failure(path)[0]

    def _serial(self, device_dir: Path) -> tuple[str | None, bool]:
        """Prefer the SCSI unit-serial VPD page; fall back to a 'serial' attr.

        Returns the same (value, read-failed) pair as _text_or_failure, because
        the serial is one of the two identifiers a caller correlates on and an
        unreadable one must not look like an absent one.
        """
        vpd = device_dir / "vpd_pg80"
        failed = False
        try:
            raw = vpd.read_bytes()
        except FileNotFoundError:
            raw = b""
        except OSError:
            # vpd_pg80, like wwid, is served from the kernel's cached VPD
            # buffer (sdev->vpd_pg80; sdev_show_wwid resolves via
            # scsi_vpd_lun_id() over vpd_pg83) that scsi_attach_vpd() fills at
            # scan/rescan - reading it does not reach the drive, so failure
            # here is our own gap (the cache torn down mid-removal, or a
            # container profile denying the sysfs read), not evidence the disk
            # stopped answering. That is exactly why "we got a model but no
            # identifiers" must not be reported as a disk without identifiers:
            # read() still revalidates whatever it returns against wwid on the
            # next call, so a stale or wrong identity cannot outlive one poll,
            # but a *dropped* identity for a disk that is still in the bay is
            # a real loss (§20) this raise exists to prevent.
            raw = b""
            failed = True
        if len(raw) > _VPD_HEADER_BYTES:
            candidate = raw[_VPD_HEADER_BYTES:].decode("ascii", errors="ignore")
            candidate = "".join(c for c in candidate if c.isprintable()).strip()
            if candidate:
                return candidate, False
        value, attr_failed = self._text_or_failure(device_dir / "serial")
        return value, failed or attr_failed

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

        Raises DiskIdentityUnreadable when the device exists but neither
        identifier could be read *and* at least one of those reads failed. An
        empty return therefore keeps one unambiguous meaning - "there is
        nothing here, or this disk declares no identifiers" - which is the
        precondition every caller that correlates identity relies on.

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

        raw_wwid, wwid_failed = self._text_or_failure(device_dir / "wwid")
        cached = self._cache.get(name)
        if cached is not None and raw_wwid is not None and cached[0] == raw_wwid:
            return cached[1]

        serial, serial_failed = self._serial(device_dir)
        wwn = self._normalise_wwn(raw_wwid)
        if wwn is None and serial is None and (wwid_failed or serial_failed):
            # Nothing identifies this disk and the silence is ours, not the
            # disk's. A cached entry cannot rescue it: the entry is validated
            # by re-reading wwid, and that is one of the reads that just
            # failed, so serving it would be asserting an identity we cannot
            # currently confirm - exactly the wwid keying's own reason for
            # existing (a re-used /dev/sdX).
            self._cache.pop(name, None)
            raise DiskIdentityUnreadable(
                f"could not read identifying attributes for {name} under {self.sysfs_root}"
            )

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
            serial=serial,
            wwn=wwn,
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
