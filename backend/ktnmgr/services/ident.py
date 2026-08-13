"""Server-side IDENT timer engine with startup reconciliation.

Design points from the spec:

§26  Timers are server-side. Closing the browser, losing the network, or
     reloading the page must not leave an LED lit forever.
§27  On startup the application must NOT blindly clear every lit IDENT. It may
     only auto-clear a request it can prove it created and whose timer has
     expired. Anything else is surfaced as external/unknown origin and left
     alone until an authenticated admin clears it.
§37  A container restart during a timed IDENT is an explicit failure case, so
     outstanding requests are persisted to disk and reloaded.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from ktnmgr.enclosure.locate import LocateError, LocateWriter, validate_request
from ktnmgr.services.audit import AuditLog

log = logging.getLogger(__name__)

#: Durations offered by the UI (§26). None means "until cleared".
ALLOWED_DURATIONS: tuple[int | None, ...] = (10, 30, 60, 300, None)

ORIGIN_APP = "app"
ORIGIN_EXTERNAL = "external"


class IdentRecord(BaseModel):
    enclosure_id: str
    ses_slot: int
    created_by: str
    created_at: datetime
    expires_at: datetime | None = None
    origin: str = ORIGIN_APP

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at


class IdentState(BaseModel):
    records: list[IdentRecord] = Field(default_factory=list)


class IdentManager:
    """Owns every IDENT request the application has issued."""

    def __init__(
        self,
        writer: LocateWriter,
        audit: AuditLog,
        state_path: Path,
        tick_seconds: float = 1.0,
    ) -> None:
        self.writer = writer
        self.audit = audit
        self.state_path = Path(state_path)
        self.tick_seconds = tick_seconds
        self._records: dict[tuple[str, int], IdentRecord] = {}
        # One write lock per enclosure (§26 step 5) so two concurrent requests
        # cannot interleave a write and its verification read.
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            state = IdentState.model_validate_json(raw)
        except ValueError:
            log.warning("ident state file %s is unreadable; ignoring", self.state_path)
            return
        self._records = {(r.enclosure_id, r.ses_slot): r for r in state.records}
        log.info("restored %d IDENT record(s) from %s", len(self._records), self.state_path)

    def _save(self) -> None:
        state = IdentState(records=list(self._records.values()))
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(state.model_dump_json(), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.error("could not persist ident state: %s", exc)

    def _lock_for(self, enclosure_id: str) -> asyncio.Lock:
        if enclosure_id not in self._locks:
            self._locks[enclosure_id] = asyncio.Lock()
        return self._locks[enclosure_id]

    # ----------------------------------------------------------------- queries

    def record_for(self, enclosure_id: str, ses_slot: int) -> IdentRecord | None:
        return self._records.get((enclosure_id.lower(), ses_slot))

    def describe(
        self, enclosure_id: str, ses_slot: int, locate_on: bool
    ) -> tuple[str | None, datetime | None]:
        """Return (origin, expires_at) for a bay given its observed locate state."""
        record = self.record_for(enclosure_id, ses_slot)
        if not locate_on:
            return (None, None)
        if record is None:
            return (ORIGIN_EXTERNAL, None)
        return (record.origin, record.expires_at)

    # ---------------------------------------------------------------- mutation

    async def identify(
        self,
        enclosure_id: str,
        ses_slot: int,
        *,
        on: bool,
        user: str,
        duration_seconds: int | None = None,
        serial: str | None = None,
    ) -> IdentRecord | None:
        """Turn IDENT on or off, verifying the result by read-back (§26).

        Returns the active record when turning on, or None when turning off.
        """
        enclosure_id, ses_slot = validate_request(enclosure_id, ses_slot)

        if on and duration_seconds not in ALLOWED_DURATIONS:
            raise LocateError(f"duration {duration_seconds!r} is not an offered value")

        async with self._lock_for(enclosure_id):
            loop = asyncio.get_running_loop()
            previous = await loop.run_in_executor(
                None, self.writer.read, enclosure_id, ses_slot
            )
            verified = await loop.run_in_executor(
                None, self.writer.write, enclosure_id, ses_slot, on
            )

            success = verified is on
            self.audit.record(
                user=user,
                enclosure=enclosure_id,
                bay=ses_slot + 1,
                ses_slot=ses_slot,
                serial=serial,
                operation="IDENT_ON" if on else "IDENT_OFF",
                previous="1" if previous else "0",
                result="1" if verified else "0",
                verification="success" if success else "failed",
                detail=None if success else "hardware did not honour the write",
            )
            if not success:
                raise LocateError(
                    f"IDENT verification failed: wrote {int(on)}, read back {int(verified)}"
                )

            key = (enclosure_id, ses_slot)
            if not on:
                self._records.pop(key, None)
                self._save()
                return None

            now = datetime.now(UTC)
            record = IdentRecord(
                enclosure_id=enclosure_id,
                ses_slot=ses_slot,
                created_by=user,
                created_at=now,
                expires_at=(now + timedelta(seconds=duration_seconds))
                if duration_seconds
                else None,
                origin=ORIGIN_APP,
            )
            self._records[key] = record
            self._save()
            return record

    # ---------------------------------------------------------- reconciliation

    async def reconcile(self, observed: dict[tuple[str, int], bool]) -> None:
        """Reconcile persisted records against reality at startup (§27).

        ``observed`` maps (enclosure_id, ses_slot) -> current locate state.

        - A record whose LED is no longer lit is dropped; something else
          cleared it.
        - A record whose timer expired while the app was down is cleared now,
          because the app can prove it created that request.
        - A lit LED with no record is left alone and reported as external.
        """
        self._load()
        now = datetime.now(UTC)

        for key, record in list(self._records.items()):
            lit = observed.get(key)
            if lit is None:
                log.info("dropping IDENT record for absent slot %s", key)
                self._records.pop(key, None)
                continue
            if not lit:
                log.info("IDENT for %s already cleared externally; dropping record", key)
                self._records.pop(key, None)
                continue
            if record.expired(now):
                log.info("IDENT for %s expired while the app was down; clearing", key)
                try:
                    await self.identify(
                        record.enclosure_id,
                        record.ses_slot,
                        on=False,
                        user="system:reconcile",
                    )
                except LocateError as exc:
                    log.error("could not clear expired IDENT %s: %s", key, exc)

        external = [k for k, lit in observed.items() if lit and k not in self._records]
        for key in external:
            log.warning(
                "IDENT active on %s with no record - external/unknown origin, leaving alone", key
            )

        self._save()

    # ------------------------------------------------------------------ timers

    async def _tick(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick_seconds)
                now = datetime.now(UTC)
                for key, record in list(self._records.items()):
                    if not record.expired(now):
                        continue
                    log.info("IDENT timer expired for %s; clearing", key)
                    try:
                        await self.identify(
                            record.enclosure_id,
                            record.ses_slot,
                            on=False,
                            user="system:timer",
                        )
                    except LocateError as exc:
                        log.error("automatic IDENT clear failed for %s: %s", key, exc)
                        # Drop the record so a permanently failing slot does not
                        # spin the timer loop every tick.
                        self._records.pop(key, None)
                        self._save()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the timer loop must never die
                log.exception("unexpected error in IDENT timer loop")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
