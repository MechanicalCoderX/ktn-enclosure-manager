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

§27's external/unknown verdict is a *composition* of two independently-clocked
facts: an observation of the hardware (which reaches this module through
StateService's slot cache, up to poll_slots_seconds old) and this module's own
records (live, in memory). Composing them without ordering information is what
made a bay the application itself lit and then cleared briefly report as
external - see ``describe`` and ``_last_write`` below.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
        clear_max_attempts: int = 5,
        clear_backoff_seconds: float = 2.0,
    ) -> None:
        self.writer = writer
        self.audit = audit
        self.state_path = Path(state_path)
        self.tick_seconds = tick_seconds
        # Retry policy for expired-IDENT auto-clears. The most likely clear
        # failure is transient - HelperLocateWriter raises LocateError while the
        # helper socket is down for a helper restart - so dropping the record on
        # the first failure would leave the LED lit forever, the exact outcome
        # §26's server-side timers exist to prevent. Retries are bounded so a
        # permanently failing slot cannot spin the timer loop forever, and the
        # delay doubles per failure (defaults: 2, 4, 8, 16 s, ~30 s in total)
        # to outlast a helper restart without hammering a dead socket.
        self.clear_max_attempts = clear_max_attempts
        self.clear_backoff_seconds = clear_backoff_seconds
        self._records: dict[tuple[str, int], IdentRecord] = {}
        #: (enclosure_id, ses_slot) -> (failed clear attempts, next retry time).
        #: In-memory only: after a container restart, reconciliation (§27)
        #: re-proves ownership and retries from scratch anyway.
        self._clear_failures: dict[tuple[str, int], tuple[int, datetime]] = {}
        #: (enclosure_id, ses_slot) -> (monotonic instant, resulting locate
        #: state) of the last write this app made to that slot AND verified by
        #: hardware read-back. It is the newest proof of a slot's IDENT state
        #: that exists anywhere in the process, so ``describe`` uses it to
        #: order a caller's cached observation against our own writes (§27).
        #: Monotonic rather than wall clock: the two timestamps are only ever
        #: compared inside one process, and an NTP step must not be able to
        #: make an observation look older or newer than it was.
        #: In-memory only, and never pruned - one entry per bay the app has
        #: ever written to, so it is bounded by the size of the shelf.
        self._last_write: dict[tuple[str, int], tuple[float, bool]] = {}
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
            # 0600: the records carry the username that raised each request.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, state.model_dump_json().encode("utf-8"))
            finally:
                os.close(fd)
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
        self, enclosure_id: str, ses_slot: int, locate_on: bool, observed_at: float
    ) -> tuple[bool, str | None, datetime | None]:
        """Return (locate, origin, expires_at) for a bay from an observation.

        ``locate_on`` is a hardware reading and ``observed_at`` is the
        ``time.monotonic()`` instant at which it was taken - not the instant it
        was cached, and not now. Callers serve readings from a cache that is up
        to one poll interval old, and the timestamp is mandatory because an
        observation whose age is unknown cannot be ordered against this
        manager's own writes; getting that ordering wrong is precisely the bug
        this argument exists to prevent.

        An observation taken at or before our last verified write to the slot
        is superseded by that write's result. The write is the stronger
        evidence: ``identify`` only reaches ``_last_write`` after the hardware
        read-back confirmed the new state, so at that instant the LED provably
        was what we say it was, while the reading describes a moment that has
        already passed.

        Without that rule the timer's auto-clear produced a bay that was
        reported as lit (from a slot cache filled before the clear) with no
        record (popped by the clear), which §27 renders as external/unknown
        origin - a warning about an LED that was already provably dark, for a
        request the application itself owned. The same false verdict is
        reachable from either side of the race: a poll whose sysfs read landed
        before the clearing write can complete after it, so simply refreshing
        the cache once the timer clears (as the manual identify route does)
        narrows the window without closing it. Ordering the two clocks closes
        it for every phase relationship.

        The rule is strictly bounded and cannot hide a genuinely external LED:
        it only ever overrides a reading OLDER than one of our own verified
        writes. An external light switched on after that write is observed by
        a later poll, and reported (§27). Only the single in-flight poll that
        straddles our write is discarded, which is exactly the reading that
        cannot be attributed to either side.
        """
        key = (enclosure_id.lower(), ses_slot)
        written_at, written_state = self._last_write.get(key, (None, False))
        if written_at is not None and observed_at <= written_at:
            locate_on = written_state

        record = self.record_for(enclosure_id, ses_slot)
        if not locate_on:
            return (False, None, None)
        if record is None:
            return (True, ORIGIN_EXTERNAL, None)
        return (True, record.origin, record.expires_at)

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
            try:
                verified = await loop.run_in_executor(
                    None, self.writer.write, enclosure_id, ses_slot, on
                )
            except Exception as exc:
                # An attempt that raised is still an attempted write, and the
                # audit log claims to record every one. Previously only writes
                # that returned were recorded, so the cases most worth having
                # a trail of - the helper refusing, the enclosure gone, a
                # permission failure - were exactly the ones that left none.
                self.audit.record(
                    user=user,
                    enclosure=enclosure_id,
                    bay=ses_slot + 1,
                    ses_slot=ses_slot,
                    serial=serial,
                    operation="IDENT_ON" if on else "IDENT_OFF",
                    previous="1" if previous else "0",
                    result=None,
                    verification="error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                raise

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
            # Any successful write supersedes retry state left by a failed
            # auto-clear: off means the LED is provably out, and on starts a
            # fresh request whose expiry must not inherit the old backoff.
            self._clear_failures.pop(key, None)

            # PUBLICATION ORDER IS LOAD-BEARING, and it differs per branch.
            #
            # describe() reads _last_write and then _records, and bays() does
            # NOT only run on the event loop: the /bays endpoint is a sync
            # `def` (Starlette runs it in the anyio threadpool) and the health
            # notifier composes bays() via run_in_executor. A reader can
            # therefore observe these two dicts between any pair of bytecodes.
            #
            # So each branch publishes first whichever half is harmless when
            # seen alone, and second the half that would fabricate an
            # external IDENT if it were seen alone:
            #
            #   off: stamp(off) alone -> the reading is overridden to dark,
            #        describe() returns "not lit". Harmless. But a popped
            #        record seen WITHOUT the stamp is precisely the stale-lit
            #        + no-record pair that reports external. Stamp first.
            #   on:  the record alone -> a still-dark reading with a record
            #        just reads as "off" for one poll. Harmless. But
            #        stamp(on) seen WITHOUT the record forces locate on with
            #        nothing to attribute it to - external again. Record
            #        first.
            if not on:
                self._last_write[key] = (time.monotonic(), False)
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
            self._last_write[key] = (time.monotonic(), True)
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
                except (LocateError, LookupError) as exc:
                    # LookupError covers a detached enclosure at startup, the
                    # same pairing as the timer loop. The record stays, so the
                    # loop's bounded clear-retry takes over from here.
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
                    attempts, retry_at = self._clear_failures.get(key, (0, now))
                    if now < retry_at:
                        # A previous clear attempt failed and its backoff window
                        # is still open; keep the record so the retry happens.
                        continue
                    log.info("IDENT timer expired for %s; clearing", key)
                    try:
                        await self.identify(
                            record.enclosure_id,
                            record.ses_slot,
                            on=False,
                            user="system:timer",
                        )
                    except (LocateError, LookupError) as exc:
                        # LocateError is what HelperLocateWriter raises while the
                        # helper socket is unreachable (e.g. mid helper restart);
                        # LookupError is the sysfs backend's signal for a detached
                        # enclosure. Both are frequently transient, and a dropped
                        # record means nothing will ever clear this LED (§26) -
                        # so retry with backoff, but only a bounded number of
                        # times so a permanently failing slot cannot spin the
                        # timer loop forever.
                        attempts += 1
                        if attempts >= self.clear_max_attempts:
                            log.error(
                                "giving up on expired IDENT %s after %d failed clear "
                                "attempts (%s) - the LED may still be lit; clear it "
                                "from the UI or on the enclosure itself",
                                key,
                                attempts,
                                exc,
                            )
                            self._records.pop(key, None)
                            self._clear_failures.pop(key, None)
                            self._save()
                        else:
                            delay = self.clear_backoff_seconds * 2 ** (attempts - 1)
                            self._clear_failures[key] = (
                                attempts,
                                now + timedelta(seconds=delay),
                            )
                            log.warning(
                                "automatic IDENT clear failed for %s "
                                "(attempt %d/%d, retrying in %.0fs): %s",
                                key,
                                attempts,
                                self.clear_max_attempts,
                                delay,
                                exc,
                            )
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
