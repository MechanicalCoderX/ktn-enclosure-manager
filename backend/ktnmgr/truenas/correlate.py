"""Correlate TrueNAS pool/disk data onto physical bays.

The join key is the block device name, because that is what the enclosure
sysfs tree gives authoritatively (``<slot>/device/block``) and what ZFS
topology reports (``child['disk']``). The block name is treated as transient:
it is used only to attach data to a bay in this poll cycle, never stored as
identity (spec §20).
"""

from __future__ import annotations

import logging
from typing import Any

from ktnmgr.models import DiskIdentity, SmartInfo, ZfsInfo, ZfsState

log = logging.getLogger(__name__)

#: Topology groups walked when attributing a disk to a vdev.
_TOPOLOGY_GROUPS = ("data", "special", "dedup", "log", "cache", "spare")


def _as_state(raw: Any) -> ZfsState:
    try:
        return ZfsState(str(raw).upper())
    except ValueError:
        return ZfsState.UNKNOWN


def _walk(node: dict[str, Any], vdev_name: str | None, out: dict[str, dict[str, Any]]) -> None:
    """Depth-first walk of a topology node, recording leaf disks.

    ``vdev_name`` is the name of the nearest enclosing non-leaf vdev, so a disk
    in ``raidz3-0`` reports that rather than its own device name.
    """
    children = node.get("children") or []
    node_type = str(node.get("type") or "").upper()

    if children:
        # A named container (RAIDZ*, MIRROR) becomes the vdev label for
        # everything beneath it. A bare DISK node has no children.
        label = node.get("name") if node_type != "DISK" else vdev_name
        for child in children:
            _walk(child, label or vdev_name, out)
        return

    disk = node.get("disk")
    if not disk:
        # unavail_disk covers a removed member that ZFS still remembers.
        disk = node.get("unavail_disk")
    if not disk:
        return

    out[str(disk)] = {
        "vdev": vdev_name or node.get("name"),
        "status": node.get("status"),
        "stats": node.get("stats") or {},
    }


def build_zfs_index(pools: list[dict[str, Any]]) -> dict[str, ZfsInfo]:
    """Map block device name -> ZfsInfo across every pool and vdev group."""
    index: dict[str, ZfsInfo] = {}

    for pool in pools:
        pool_name = str(pool.get("name") or "")
        topology = pool.get("topology") or {}

        scan = pool.get("scan") or {}
        resilvering = (
            str(scan.get("function") or "").upper() == "RESILVER"
            and str(scan.get("state") or "").upper() in ("SCANNING", "ACTIVE")
        )

        for group in _TOPOLOGY_GROUPS:
            for vdev in topology.get(group) or []:
                leaves: dict[str, dict[str, Any]] = {}
                _walk(vdev, vdev.get("name"), leaves)
                for disk_name, detail in leaves.items():
                    stats = detail.get("stats") or {}
                    index[disk_name] = ZfsInfo(
                        pool=pool_name,
                        vdev=detail.get("vdev"),
                        state=_as_state(detail.get("status")),
                        read_errors=stats.get("read_errors"),
                        write_errors=stats.get("write_errors"),
                        checksum_errors=stats.get("checksum_errors"),
                        is_spare=(group == "spare"),
                        resilvering=resilvering,
                    )

    return index


def build_disk_index(disks: list[dict[str, Any]]) -> dict[str, DiskIdentity]:
    """Map block device name -> DiskIdentity from ``disk.query``.

    Only the fields this application displays are read; nothing else from the
    record is retained or forwarded to the browser.
    """
    index: dict[str, DiskIdentity] = {}
    for record in disks:
        name = record.get("name") or record.get("devname")
        if not name:
            continue
        model = record.get("model")
        size = record.get("size")
        rotation = record.get("rotationrate")
        index[str(name)] = DiskIdentity(
            serial=record.get("serial") or None,
            wwn=_normalise_lunid(record.get("lunid")),
            model=str(model) if model else None,
            size_bytes=int(size) if isinstance(size, (int, float)) else None,
            transport=record.get("bus"),
            rotational=(str(record.get("type") or "").upper() == "HDD")
            if record.get("type")
            else (bool(rotation) if rotation is not None else None),
        )
    return index


def _normalise_lunid(lunid: Any) -> str | None:
    if not lunid:
        return None
    value = str(lunid).strip().lower()
    return value if value.startswith("0x") else f"0x{value}"


def build_smart_index(
    temperatures: dict[str, float | None],
    disks: list[dict[str, Any]] | None = None,
) -> dict[str, SmartInfo]:
    """Map block device name -> SmartInfo.

    ``disk.temperatures`` returns ``None`` for disks it could not read, which
    is normal and must not be rendered as 0 C.
    """
    index: dict[str, SmartInfo] = {}
    for name, temperature in temperatures.items():
        index[str(name)] = SmartInfo(
            temperature_c=temperature,
            available=temperature is not None,
        )
    for record in disks or []:
        name = str(record.get("name") or "")
        if name and name not in index:
            index[name] = SmartInfo(available=False)
    return index


def merge_identity(local: DiskIdentity, remote: DiskIdentity | None) -> DiskIdentity:
    """Prefer locally-read sysfs values, filling gaps from TrueNAS.

    Local sysfs is authoritative for firmware and WWN because it needs no
    network call and stays correct when the API is down (§37).
    """
    if remote is None:
        return local
    merged = local.model_copy()
    for field in ("serial", "wwn", "model", "firmware", "size_bytes", "transport", "rotational"):
        if getattr(merged, field, None) in (None, ""):
            value = getattr(remote, field, None)
            if value not in (None, ""):
                setattr(merged, field, value)
    return merged
