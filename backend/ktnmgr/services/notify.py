"""Health-change notifications.

The application already knows the moment a bay degrades - it polls ZFS state,
error counters and enclosure status every cycle. Without this module it only
tells you if you happen to be looking at the page, which is the wrong time.

Notifications fire on a *transition*, never on a steady state, so a permanently
degraded pool does not produce a message every poll. The last observed health
per bay is persisted, so restarting the app does not re-announce conditions the
operator has already been told about.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from ktnmgr.models import Bay, SlotHealth

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0

#: Health values that warrant telling somebody.
BAD = (SlotHealth.WARNING, SlotHealth.FAILED)

#: ntfy priority and tags per health, so a phone can filter on severity.
_NTFY_STYLE: dict[str, tuple[str, str]] = {
    SlotHealth.FAILED: ("urgent", "rotating_light"),
    SlotHealth.WARNING: ("high", "warning"),
    SlotHealth.OK: ("default", "white_check_mark"),
    SlotHealth.EMPTY: ("low", "heavy_minus_sign"),
    SlotHealth.UNKNOWN: ("default", "grey_question"),
}


class HealthNotifier:
    """Sends a message when a bay's health changes."""

    def __init__(
        self,
        url: str,
        style: str = "ntfy",
        state_path: Path | None = None,
        notify_recovery: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/") if url else ""
        self.style = style if style in ("ntfy", "json") else "ntfy"
        self.state_path = Path(state_path) if state_path else None
        self.notify_recovery = notify_recovery
        self.timeout = timeout
        self._last: dict[str, str] = {}
        self._loaded = False

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    # ---------------------------------------------------------- persistence

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.state_path is None:
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._last = {str(k): str(v) for k, v in data.items()}

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._last), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("could not persist notifier state: %s", exc)

    # -------------------------------------------------------------- message

    @staticmethod
    def _describe(bay: Bay) -> str:
        bits = [f"Bay {bay.display_bay} (SES slot {bay.ses_slot})"]
        if bay.disk.serial:
            bits.append(f"serial {bay.disk.serial}")
        if bay.device:
            bits.append(bay.device)
        if bay.zfs.pool:
            bits.append(f"{bay.zfs.pool}/{bay.zfs.vdev}")
        if bay.zfs.state and bay.zfs.state != "UNKNOWN":
            bits.append(f"ZFS {bay.zfs.state}")
        errors = [
            f"{name}={value}"
            for name, value in (
                ("read", bay.zfs.read_errors),
                ("write", bay.zfs.write_errors),
                ("cksum", bay.zfs.checksum_errors),
            )
            if value
        ]
        if errors:
            bits.append("errors " + " ".join(errors))
        if bay.smart.temperature_c is not None:
            bits.append(f"{bay.smart.temperature_c:.0f}C")
        return ", ".join(bits)

    async def _send(self, bay: Bay, previous: str | None) -> None:
        health = SlotHealth(bay.health)
        priority, tag = _NTFY_STYLE.get(health, ("default", "grey_question"))
        recovered = health is SlotHealth.OK
        # Uses the same noun the UI shows on the tile ("Bay 8"), so an alert
        # and the screen the operator then opens agree with each other.
        title = (
            f"Bay {bay.display_bay} recovered"
            if recovered
            else f"Bay {bay.display_bay} is {health.value.upper()}"
        )
        body = self._describe(bay)
        if previous:
            body = f"{body}\nwas: {previous} -> now: {health.value}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                if self.style == "ntfy":
                    await client.post(
                        self.url,
                        content=body.encode("utf-8"),
                        headers={"Title": title, "Priority": priority, "Tags": tag},
                    )
                else:
                    await client.post(
                        self.url,
                        json={
                            "title": title,
                            "message": body,
                            "health": health.value,
                            "previous": previous,
                            "enclosure": bay.enclosure_id,
                            "bay": bay.display_bay,
                            "ses_slot": bay.ses_slot,
                            "serial": bay.disk.serial,
                            "device": bay.device,
                            "pool": bay.zfs.pool,
                            "vdev": bay.zfs.vdev,
                            "zfs_state": bay.zfs.state,
                        },
                    )
        except (httpx.HTTPError, OSError) as exc:
            # A failing notification endpoint must never disturb polling.
            log.warning("could not deliver health notification: %s", exc)
            return
        log.info("notified: %s", title)

    # ------------------------------------------------------------- evaluate

    async def evaluate(self, bays: list[Bay]) -> None:
        """Compare current health against the last observation and notify."""
        if not self.enabled:
            return
        self._load()

        changed = False
        for bay in bays:
            key = f"{bay.enclosure_id}:{bay.ses_slot}"
            health = SlotHealth(bay.health)
            previous = self._last.get(key)

            if previous == health.value:
                continue

            self._last[key] = health.value
            changed = True

            if health in BAD:
                # Fires on first observation too: a drive already failed when
                # the app starts is exactly what an operator needs told.
                await self._send(bay, previous)
            elif previous is not None and previous in {h.value for h in BAD}:
                if self.notify_recovery:
                    await self._send(bay, previous)

        if changed:
            self._save()
