"""Disk identity caching must not survive a device-name reuse.

This is not hypothetical. On the validation system a replacement drive was
assigned the same /dev/sdf the removed drive had held, so a cache keyed on the
device name alone would have shown the previous drive's serial against the new
disk - the precise confusion this application exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from ktnmgr.enclosure.disks import DiskInfoReader


def make_disk(root: Path, name: str, wwid: str, serial: str, model: str = "HITACHI X") -> None:
    block = root / "block" / name
    device = block / "device"
    device.mkdir(parents=True, exist_ok=True)
    (block / "size").write_text("5860533168\n")
    (block / "queue").mkdir(exist_ok=True)
    (block / "queue" / "rotational").write_text("1\n")
    (device / "wwid").write_text(f"naa.{wwid}\n")
    (device / "vendor").write_text("HITACHI\n")
    (device / "model").write_text(f"{model}\n")
    (device / "rev").write_text("C370\n")
    (device / "vpd_pg80").write_bytes(b"\x00\x80\x00\x08" + serial.encode())


def test_identity_is_cached_between_reads(tmp_path: Path) -> None:
    make_disk(tmp_path, "sdf", "5000cca0000000aa", "SERIAL-A")
    reader = DiskInfoReader(sysfs_root=tmp_path)

    first = reader.read("sdf")
    second = reader.read("sdf")

    assert first.serial == "SERIAL-A"
    assert second.serial == "SERIAL-A"
    assert second is first, "identity was recomposed instead of served from cache"


def test_same_device_name_with_a_different_disk_is_not_served_from_cache(
    tmp_path: Path,
) -> None:
    make_disk(tmp_path, "sdf", "5000cca0000000aa", "OLD-DRIVE")
    reader = DiskInfoReader(sysfs_root=tmp_path)
    assert reader.read("sdf").serial == "OLD-DRIVE"

    # The drive is pulled and a different one lands on the same name.
    make_disk(tmp_path, "sdf", "5000cca0000000bb", "NEW-DRIVE")

    refreshed = reader.read("sdf")
    assert refreshed.serial == "NEW-DRIVE", "stale identity served after a device-name reuse"
    assert refreshed.wwn == "0x5000cca0000000bb"


def test_disk_without_a_wwid_is_not_cached(tmp_path: Path) -> None:
    """No wwid means no cheap way to detect a swap, so never cache it."""
    make_disk(tmp_path, "sdg", "5000cca0000000cc", "SERIAL-C")
    (tmp_path / "block" / "sdg" / "device" / "wwid").unlink()

    reader = DiskInfoReader(sysfs_root=tmp_path)
    first = reader.read("sdg")
    second = reader.read("sdg")

    assert first.serial == "SERIAL-C"
    assert second is not first, "an unidentifiable disk was cached"


def test_disappearing_device_clears_its_cache_entry(tmp_path: Path) -> None:
    make_disk(tmp_path, "sdh", "5000cca0000000dd", "SERIAL-D")
    reader = DiskInfoReader(sysfs_root=tmp_path)
    assert reader.read("sdh").serial == "SERIAL-D"

    for child in sorted((tmp_path / "block" / "sdh").rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    (tmp_path / "block" / "sdh").rmdir()

    assert reader.read("sdh").serial is None
    assert "sdh" not in reader._cache
