"""Health-change notifications.

The application already knows the moment a bay degrades - it polls ZFS state,
error counters and enclosure status every cycle. Without this module it only
tells you if you happen to be looking at the page, which is the wrong time.

Notifications fire on a *transition*, never on a steady state, so a permanently
degraded pool does not produce a message every poll. What gets persisted per
bay is the last health the operator was actually *told about*: an alert-worthy
transition is only recorded once its message was accepted by the endpoint, so
a delivery failure is retried on later evaluations instead of silently
committing - the network hiccup that degrades a pool is exactly the hiccup
likely to eat the webhook that reports it. Restarting the app does not
re-announce conditions the operator has already been told about.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from ktnmgr.models import Bay, SlotHealth

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0

#: Minimum spacing between delivery attempts for the same undelivered alert.
#: Retrying on every poll tick against a dead endpoint would stall the loop by
#: a full connect timeout each cycle - the exact disturbance this module
#: promises never to cause - while a drive-failure alert arriving a minute
#: late is still an alert that arrived.
DEFAULT_RETRY_SECONDS = 60.0

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
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> None:
        self.url = url.rstrip("/") if url else ""
        self.style = style if style in ("ntfy", "json") else "ntfy"
        self.state_path = Path(state_path) if state_path else None
        self.notify_recovery = notify_recovery
        self.timeout = timeout
        self.retry_seconds = retry_seconds
        self._last: dict[str, str] = {}
        #: Bays with an undelivered alert, mapped to the monotonic time before
        #: which no re-attempt is made. Entries are dropped the moment the
        #: alert is delivered or its transition is superseded, so the map can
        #: never outgrow the set of bays currently stuck in an alert-worthy
        #: state - retry bookkeeping stays bounded however long an endpoint is
        #: down, same discipline as the audit log's bounded tail.
        self._retry_at: dict[str, float] = {}
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

    async def _send(self, bay: Bay, previous: str | None) -> bool:
        """Attempt delivery; True only when the endpoint accepted the message.

        The verdict gates the state commit in evaluate(): a transition whose
        alert was not accepted must stay uncommitted so it is retried.
        """
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
                    response = await client.post(
                        self.url,
                        content=body.encode("utf-8"),
                        headers={"Title": title, "Priority": priority, "Tags": tag},
                    )
                else:
                    response = await client.post(
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
            return False

        # A 404 from a mistyped ntfy topic, or a 401 from a webhook that wants
        # auth, is not delivery. Without this the log said "notified" forever
        # while nothing ever arrived - the worst possible failure mode for an
        # alerting path, because it looks healthy.
        if response.status_code >= 400:
            log.warning(
                "health notification rejected: HTTP %s from %s",
                response.status_code, self.url,
            )
            return False
        log.info("notified: %s", title)
        return True

    # ------------------------------------------------------------- evaluate

    async def evaluate(self, bays: list[Bay]) -> None:
        """Compare current health against the last *delivered* state and notify."""
        if not self.enabled:
            return
        self._load()

        now = time.monotonic()
        changed = False
        # (key, new health value, bay, previous): transitions whose commit is
        # deferred until _send() reports the endpoint accepted the alert.
        pending: list[tuple[str, str, Bay, str | None]] = []
        bad_values = {h.value for h in BAD}

        for bay in bays:
            key = f"{bay.enclosure_id}:{bay.ses_slot}"
            health = SlotHealth(bay.health)
            previous = self._last.get(key)

            if previous == health.value:
                # Nothing outstanding for this bay: either its alert was
                # delivered and committed, or the bay moved back to the old
                # state before delivery ever succeeded and the stale alert is
                # superseded. Dropping the cooldown here means the *next* real
                # transition gets an immediate first attempt.
                self._retry_at.pop(key, None)
                continue

            # First clause fires on first observation too: a drive already
            # failed when the app starts is exactly what an operator needs
            # told.
            wants_alert = health in BAD or (
                previous is not None and previous in bad_values and self.notify_recovery
            )

            if not wants_alert:
                # A silent transition (UNKNOWN -> OK, a pulled drive going
                # EMPTY, a recovery with recovery alerts off) has nothing to
                # deliver, so it commits immediately - and supersedes any
                # undelivered alert for this bay, so a dead endpoint can never
                # wedge the bay's state behind an alert that no longer
                # describes reality.
                self._last[key] = health.value
                self._retry_at.pop(key, None)
                changed = True
                continue

            if now < self._retry_at.get(key, 0.0):
                # A recent attempt for this bay failed; leave the transition
                # uncommitted and try again after the cooldown (see
                # DEFAULT_RETRY_SECONDS for why not every poll tick).
                continue

            pending.append((key, health.value, bay, previous))

        # Sent concurrently, not one after another. Losing the TrueNAS
        # connection changes every bay at once, and serial delivery against a
        # dead endpoint meant 15 bays x the 10s timeout - two and a half
        # minutes of stalled polling caused by the notifier, which is supposed
        # to be incapable of disturbing it.
        if pending:
            results = await asyncio.gather(
                *(self._send(bay, previous) for _, _, bay, previous in pending)
            )
            for (key, value, _bay, _previous), delivered in zip(pending, results, strict=True):
                if delivered:
                    self._last[key] = value
                    self._retry_at.pop(key, None)
                    changed = True
                else:
                    # Deliberately NOT committed (and not persisted): the next
                    # evaluation still sees the old state and retries with the
                    # original transition context. Committing here is how a
                    # failed-drive alert used to vanish forever when the
                    # webhook rode the same network hiccup that degraded the
                    # pool.
                    self._retry_at[key] = now + self.retry_seconds

        if changed:
            self._save()
