"""The spurious "external/unknown origin" IDENT banner (spec §26, §27).

Reported against v1.5.4 on the validation shelf: Identify a bay with a timer,
and around the moment the timer auto-clears the UI shows

    IDENT active - external/unknown origin. This was not started by this
    application; it is left alone until you clear it.

for a request the application itself had just made and had just, provably,
cleared - while reporting the bay as lit, though the clearing write had already
been verified by hardware read-back.

Nothing in the IDENT records is wrong. ``bays()`` composes each bay from two
sources on two different clocks - the slot cache (a sysfs snapshot up to
poll_slots_seconds old) and the live records - and the auto-clear lands between
them. ``identify(on=False)`` verifies the LED is out and pops the record, while
the cached snapshot still says the slot is lit; ``describe()`` then sees "lit,
no record", which §27 defines as external.

The race is structural rather than unlucky. Every offered duration (10/30/60/
300) is an exact multiple of poll_slots_seconds (5.0), and the manual identify
route re-phases the poll clock by refreshing the cache right after its write,
so an expiry always falls a fraction of a second either side of a scheduled
poll. Which side it falls on decides only whether the false banner lasts 20ms
or a full poll interval - so these tests drive both phase relationships
explicitly, including the one no amount of cache-refreshing can fix: a poll
whose sysfs read landed BEFORE the clearing write but whose result is cached
AFTER it.

The other half of the contract matters just as much. §27 exists to stop the app
clearing an LED somebody else lit, and the UI banner is how that reaches the
operator. Every "must still warn" test below is a guard against fixing the
false positive by blunting the true one.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ktnmgr.enclosure.locate import LocateError
from ktnmgr.models import DiskIdentity, EnclosureRef, SlotState
from ktnmgr.services.audit import AuditLog
from ktnmgr.services.ident import ORIGIN_APP, ORIGIN_EXTERNAL, IdentManager
from ktnmgr.services.state import StateService

LOGICAL_ID = "0x50060480aabbcc00"
#: SES slot 1 is bay 2 - the bay in the report.
SES_SLOT = 1

REF = EnclosureRef(
    logical_id=LOGICAL_ID,
    vendor="EMC",
    product="ESES Enclosure",
    revision="0001",
    scsi_address="1:0:15:0",
    sysfs_path="/sys/class/enclosure/1:0:15:0",
    sg_device="/dev/sg16",
    slot_count=15,
)


class FakeWriter:
    """In-memory LED, standing in for the helper's SEND DIAGNOSTIC."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, int], bool] = {}

    def read(self, enclosure_id: str, slot: int) -> bool:
        return self.state.get((enclosure_id, slot), False)

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        self.state[(enclosure_id, slot)] = on
        return self.state[(enclosure_id, slot)]


class OffRefusingWriter(FakeWriter):
    """Lights an LED, then loses the ability to put it out - the helper socket
    going away between the request and its expiry.

    Reaches the give-up path (§26), where the record is dropped after
    clear_max_attempts failures with the LED still lit. That bay really is
    beyond the app's control and really must warn.
    """

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        if not on:
            raise LocateError("helper socket unreachable")
        return super().write(enclosure_id, slot, on)


class SamplingBackend:
    """A sysfs backend that samples the LED at the instant read_slots RUNS.

    That property is where the bug lives: read_slots() executes in a worker
    thread, so the value it captures can be older than the cache entry it
    eventually lands in. ``gate`` holds a read open so a write can be issued and
    verified while a poll is still in flight, which is the interleaving a
    post-clear cache refresh cannot repair.
    """

    def __init__(self, writer: FakeWriter) -> None:
        self.writer = writer
        self.gate: threading.Event | None = None
        self.sampled = threading.Event()

    def discover(self) -> list[EnclosureRef]:
        return [REF]

    def read_slots(self, ref: EnclosureRef) -> list[SlotState]:
        locate = self.writer.read(ref.logical_id, SES_SLOT)
        self.sampled.set()
        if self.gate is not None:
            assert self.gate.wait(5.0), "gated read was never released"
        return [
            SlotState(
                ses_slot=SES_SLOT,
                display_bay=SES_SLOT + 1,
                status="OK",
                locate=locate,
                block_device="sdb",
                sysfs_path=f"{REF.sysfs_path}/{SES_SLOT}",
            )
        ]


class FakeSettings:
    poll_slots_seconds = 5.0

    def allowed_enclosures(self) -> set[str]:
        return set()


class FakeDiskReader:
    def read(self, name: str | None) -> DiskIdentity:
        return DiskIdentity(serial="SERIAL1", wwn="0x5000cca000000001")


class Harness:
    def __init__(self, writer: FakeWriter, tmp_path: Path, tick_seconds: float) -> None:
        self.writer = writer
        self.backend = SamplingBackend(writer)
        self.ident = IdentManager(
            writer=writer,
            audit=AuditLog(tmp_path / "audit.log"),
            state_path=tmp_path / "ident.json",
            tick_seconds=tick_seconds,
            clear_backoff_seconds=0.01,
        )
        self.service = StateService(
            settings=FakeSettings(),
            backend=self.backend,
            disks=FakeDiskReader(),
            ses=None,
            ident=self.ident,
            truenas=None,
        )

    def bay(self) -> Any:
        (bay,) = self.service.bays(LOGICAL_ID)
        return bay

    def cached_locate(self) -> bool:
        """What the raw slot cache says, before IdentManager corrects it."""
        return bool(self.service.slots.value[LOGICAL_ID][0].locate)

    async def light(self, duration_seconds: int | None = 10) -> None:
        await self.ident.identify(
            LOGICAL_ID, SES_SLOT, on=True, user="admin", duration_seconds=duration_seconds
        )

    async def auto_clear(self) -> None:
        """Exactly what the expiry timer does (ident.py _tick)."""
        await self.ident.identify(LOGICAL_ID, SES_SLOT, on=False, user="system:timer")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(FakeWriter(), tmp_path, tick_seconds=0.02)


def _assert_ours_and_dark(bay: Any) -> None:
    """The only honest report for a bay we lit and then verifiably cleared."""
    assert bay.ident_origin is None, (
        "a bay this application lit and then cleared must never be reported as "
        f"{bay.ident_origin!r} origin"
    )
    assert bay.locate is False, "the clearing write was verified by read-back; the LED is out"
    assert bay.ident_expires_at is None


# ----------------------------------------- the false positive, in every phase


async def test_clear_wins_the_race_against_a_stale_slot_cache(harness: Harness) -> None:
    """Phase 1: the timer clears between two polls (the user's own run).

    The cache holds a snapshot taken while the LED was lit and no poll has run
    since, so slot.locate is True while the record is already gone.
    """
    await harness.light()
    await harness.service.poll_hardware()
    assert harness.bay().ident_origin == ORIGIN_APP

    await harness.auto_clear()

    assert harness.cached_locate() is True, "precondition: the cache is genuinely stale"
    assert harness.ident.record_for(LOGICAL_ID, SES_SLOT) is None
    _assert_ours_and_dark(harness.bay())


async def test_poll_in_flight_across_the_clear_is_not_external(harness: Harness) -> None:
    """Phase 2: a poll reads the lit LED, then the clear completes, then the
    poll caches its now-obsolete reading.

    This is the interleaving that rules out "just refresh the cache when the
    timer clears, like the manual route does": the refresh would be this very
    poll, and it would still store True.
    """
    await harness.light()
    await harness.service.poll_hardware()

    harness.backend.gate = threading.Event()
    harness.backend.sampled.clear()
    poll = asyncio.create_task(harness.service.poll_hardware())
    while not harness.backend.sampled.is_set():
        await asyncio.sleep(0.01)

    await harness.auto_clear()
    harness.backend.gate.set()
    await poll

    assert harness.cached_locate() is True, (
        "precondition: the poll must have cached the pre-clear reading"
    )
    _assert_ours_and_dark(harness.bay())


async def test_poll_after_the_clear_settles_the_bay(harness: Harness) -> None:
    """Phase 3: the ordinary case, and the proof that the correction lets go.

    Once a poll has observed the dark slot the override is irrelevant - the
    reading is newer than our write and is taken at face value.
    """
    await harness.light()
    await harness.service.poll_hardware()
    await harness.auto_clear()
    await harness.service.poll_hardware()

    assert harness.cached_locate() is False
    _assert_ours_and_dark(harness.bay())


async def test_manual_clear_gets_the_same_guarantee(harness: Harness) -> None:
    """The Clear button pops the record on the same code path, so a stale
    cache lies about it identically. The route happens to refresh afterwards;
    the guarantee must not depend on the caller remembering to."""
    await harness.light(duration_seconds=None)
    await harness.service.poll_hardware()
    await harness.ident.identify(LOGICAL_ID, SES_SLOT, on=False, user="admin")

    assert harness.cached_locate() is True
    _assert_ours_and_dark(harness.bay())


async def test_whole_auto_clear_cycle_never_reports_external(tmp_path: Path) -> None:
    """The end-to-end shape of the user's session: the real timer clearing a
    real record while a real poll loop runs, sampled continuously.

    A settled-state assertion would prove nothing here - the false banner was
    only ever visible for at most one poll interval - so every sample taken
    across the cycle is checked, and the run is only accepted if the sampler
    actually visited the dangerous window (the control assertion at the end).
    """
    poll_interval = 0.2
    harness = Harness(FakeWriter(), tmp_path, tick_seconds=0.02)

    async def poller() -> None:
        while True:
            await harness.service.poll_hardware()
            await asyncio.sleep(poll_interval)

    await harness.service.poll_hardware()
    await harness.light(duration_seconds=10)
    await harness.service.poll_hardware()

    pump = asyncio.create_task(poller())
    harness.ident.start()
    samples: list[tuple[bool, bool, bool, str | None]] = []
    try:
        # Fast-forward rather than waiting out the real duration (§26 timers
        # are wall-clock, so this is how the suite already exercises expiry).
        harness.ident._records[(LOGICAL_ID, SES_SLOT)].expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        cleared_at: float | None = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            bay = harness.bay()
            record_gone = harness.ident.record_for(LOGICAL_ID, SES_SLOT) is None
            samples.append((harness.cached_locate(), record_gone, bay.locate, bay.ident_origin))
            if record_gone and cleared_at is None:
                cleared_at = time.monotonic()
            # Keep sampling well past the clear, so the poll that finally
            # observes the dark slot is inside the sampled range too.
            if cleared_at is not None and time.monotonic() - cleared_at > 3 * poll_interval:
                break
            await asyncio.sleep(0.005)
    finally:
        pump.cancel()
        await harness.ident.stop()

    assert harness.writer.read(LOGICAL_ID, SES_SLOT) is False, "the timer must have cleared it"
    external = [s for s in samples if s[3] == ORIGIN_EXTERNAL]
    assert not external, f"{len(external)}/{len(samples)} samples reported an external IDENT"
    lit_after_clear = [s for s in samples if s[1] and s[2]]
    assert not lit_after_clear, "reported lit after a verified clear"
    # Control: a run that never sampled a stale-cache moment would satisfy the
    # assertions above without testing anything at all.
    assert any(stale and gone for stale, gone, _, _ in samples), (
        "the sampler never caught the window this test exists to cover"
    )


async def test_stale_cache_does_not_hide_a_bay_we_just_lit(harness: Harness) -> None:
    """The same defect from the other side, and the reason the correction
    returns the locate state rather than only the origin: a snapshot taken
    before the ON write must not report a lit bay as dark, which would drop the
    countdown and disable the Clear button for a bay whose LED is on."""
    await harness.service.poll_hardware()
    assert harness.bay().locate is False

    await harness.light(duration_seconds=60)

    assert harness.cached_locate() is False, "precondition: the cache predates the write"
    bay = harness.bay()
    assert bay.locate is True
    assert bay.ident_origin == ORIGIN_APP
    assert bay.ident_expires_at is not None, "the UI countdown must survive a stale cache"


# ------------------------------------------- the true positive, still intact


async def test_ident_lit_by_something_else_still_warns(harness: Harness) -> None:
    """§27: an LED this app never lit is external, and stays that way."""
    harness.writer.state[(LOGICAL_ID, SES_SLOT)] = True
    await harness.service.poll_hardware()

    bay = harness.bay()
    assert bay.locate is True
    assert bay.ident_origin == ORIGIN_EXTERNAL


async def test_external_light_after_our_own_clear_still_warns(harness: Harness) -> None:
    """The case that kills any "suppress the banner for a while after a clear"
    shortcut: another host lights the very slot we just finished using. Only
    readings OLDER than our write may be overridden, so the next poll reports
    it exactly as §27 requires."""
    await harness.light()
    await harness.service.poll_hardware()
    await harness.auto_clear()
    _assert_ours_and_dark(harness.bay())

    # Somebody else lights the same bay a moment later.
    harness.writer.state[(LOGICAL_ID, SES_SLOT)] = True
    await harness.service.poll_hardware()

    bay = harness.bay()
    assert bay.locate is True
    assert bay.ident_origin == ORIGIN_EXTERNAL
    assert bay.ident_expires_at is None


async def test_orphaned_led_after_a_failed_auto_clear_still_warns(tmp_path: Path) -> None:
    """The give-up path (§26): after clear_max_attempts failures the record is
    dropped with the LED still lit. That bay genuinely is beyond the app's
    control, and external/unknown is the truthful report - the last write we
    verified there turned it ON, so nothing overrides the reading."""
    harness = Harness(OffRefusingWriter(), tmp_path, tick_seconds=0.02)
    harness.ident.clear_max_attempts = 2
    await harness.light(duration_seconds=10)
    await harness.service.poll_hardware()

    harness.ident.start()
    # Fast-forward the expiry rather than waiting it out, as the rest of the
    # suite does; every clear attempt from here will raise.
    harness.ident._records[(LOGICAL_ID, SES_SLOT)].expires_at = datetime.now(UTC) - timedelta(
        seconds=1
    )
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if harness.ident.record_for(LOGICAL_ID, SES_SLOT) is None:
                break
    finally:
        await harness.ident.stop()

    assert harness.ident.record_for(LOGICAL_ID, SES_SLOT) is None, "the app gave up"
    assert harness.writer.read(LOGICAL_ID, SES_SLOT) is True, "the LED really is still lit"
    await harness.service.poll_hardware()

    bay = harness.bay()
    assert bay.locate is True
    assert bay.ident_origin == ORIGIN_EXTERNAL


async def test_led_from_a_previous_instance_still_warns(tmp_path: Path) -> None:
    """§27 after a restart: the records file is gone, so the app cannot prove
    it owns the lit LED and must not pretend otherwise. A fresh process has no
    write history either, so there is nothing to override the reading with."""
    first = Harness(FakeWriter(), tmp_path, tick_seconds=0.02)
    await first.light(duration_seconds=None)

    second = Harness(first.writer, tmp_path / "restarted", tick_seconds=0.02)
    await second.service.poll_hardware()

    bay = second.bay()
    assert bay.locate is True
    assert bay.ident_origin == ORIGIN_EXTERNAL


# ------------------------------------------------- the ordering rule itself


def test_describe_overrides_only_readings_older_than_our_write(harness: Harness) -> None:
    """The rule in isolation, on explicit clock values: our verified write wins
    over anything observed at or before it, and loses to everything after."""
    written_at = time.monotonic()
    harness.ident._last_write[(LOGICAL_ID, SES_SLOT)] = (written_at, False)

    # A reading from before the clear is superseded by it...
    assert harness.ident.describe(
        LOGICAL_ID, SES_SLOT, locate_on=True, observed_at=written_at - 0.001
    ) == (False, None, None)
    # ...as is one that cannot be ordered against it at all.
    assert harness.ident.describe(
        LOGICAL_ID, SES_SLOT, locate_on=True, observed_at=written_at
    ) == (False, None, None)
    # A later reading is the newer evidence and is believed.
    locate, origin, _ = harness.ident.describe(
        LOGICAL_ID, SES_SLOT, locate_on=True, observed_at=written_at + 0.001
    )
    assert (locate, origin) == (True, ORIGIN_EXTERNAL)


def test_describe_without_a_write_history_trusts_the_reading(harness: Harness) -> None:
    """No write of ours, nothing to override with - including for a slot whose
    neighbour we have written to."""
    harness.ident._last_write[(LOGICAL_ID, 0)] = (time.monotonic(), False)
    locate, origin, _ = harness.ident.describe(
        LOGICAL_ID, SES_SLOT, locate_on=True, observed_at=0.0
    )
    assert (locate, origin) == (True, ORIGIN_EXTERNAL)


# ------------------------------------------------- publication order (threads)


class _ObservingDict(dict):
    """Runs a probe every time a key is published or removed.

    ``bays()`` does not only run on the event loop: the /bays endpoint is a
    sync ``def`` (Starlette hands those to the anyio threadpool) and the health
    notifier composes bays() through run_in_executor. A reader can therefore
    land between any two statements of ``identify``'s publication block, so the
    two dicts it writes must never be observable in a combination that
    fabricates an external IDENT. This stands in for that reader.
    """

    def __init__(self, probe) -> None:
        super().__init__()
        self._probe = probe

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self._probe()

    def pop(self, key, *default):
        result = super().pop(key, *default)
        self._probe()
        return result


def _probe_origins(manager: IdentManager, stale_at: float) -> list[str | None]:
    """Origins a reader holding a pre-write reading of 'lit' would compute."""
    seen: list[str | None] = []

    def probe() -> None:
        _, origin, _ = manager.describe(LOGICAL_ID, SES_SLOT, True, stale_at)
        seen.append(origin)

    return seen, probe  # type: ignore[return-value]


@pytest.mark.parametrize("turning_on", [True, False])
async def test_publication_order_never_exposes_a_fabricated_external(
    tmp_path: Path, turning_on: bool
) -> None:
    """Neither half of a write may be visible without the half that explains it.

    On the OFF path the stamp must land before the record is dropped; on the ON
    path the record must land before the stamp. Publishing them the other way
    round leaves a window - real, because these dicts are read from threadpool
    threads - in which a stale 'lit' reading has nothing to attribute it to and
    is reported as external/unknown origin (§27), which is the very banner this
    module's ordering exists to prevent.
    """
    harness = Harness(FakeWriter(), tmp_path, tick_seconds=0.01)
    manager = harness.ident
    if not turning_on:
        await harness.light(duration_seconds=None)

    stale_at = time.monotonic()  # a reading taken before the write below
    seen, probe = _probe_origins(manager, stale_at)
    observing = _ObservingDict(probe)
    observing.update(manager._records)
    manager._records = observing  # type: ignore[assignment]
    manager._last_write = _ObservingDict(probe)  # type: ignore[assignment]

    if turning_on:
        await harness.light(duration_seconds=None)
    else:
        await manager.identify(LOGICAL_ID, SES_SLOT, on=False, user="system:timer")

    assert seen, "the probe never ran - the publication block changed shape"
    assert ORIGIN_EXTERNAL not in seen, (
        f"a reader between publications saw {seen}, fabricating an external IDENT"
    )
