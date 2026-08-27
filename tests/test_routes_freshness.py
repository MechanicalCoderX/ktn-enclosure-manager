"""What the HTTP surface is allowed to assert about facts on different clocks.

``StateService.bays()`` composes sources sampled at 5 s (slots), 20 s (TrueNAS),
30 s (SES) and 120 s (SMART) intervals against live sysfs identity and live
IDENT records. The composition is right for a bay tile, which is repainted a few
seconds later. Three places in the API layer treated it as if it were right for
something else:

* the **audit serial** (§34) was lifted out of that join and then persisted to
  an append-only 0600 file. A tile can be transiently wrong; an audit row cannot
  - it is the artifact that answers "which physical drive did we act on", and it
  outlives by months the glitch that produced it.
* the **slot-cache error** was published on /api/diagnostics and nowhere else,
  so /bays looked exactly as fresh while sysfs was unreadable as it does when
  everything works (§37: a degraded source must degrade visibly).
* the **read-your-own-writes refresh** in the identify route was unguarded, so a
  refresh failure was reported to the caller as a failed IDENT write - for a
  write that had already been verified by hardware read-back, persisted and
  audited.

These tests drive the real sysfs fixture tree through the real app, and mutate
that tree between the poll and the request to create the staleness on purpose.
``poll_slots_seconds`` is set far beyond any test's lifetime so the cache goes
stale and *stays* stale: a background re-poll would repair the very condition
under test and every assertion below would pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
LOGICAL_ID = "0x50060480aabbcc00"
PASSWORD = "a-sufficiently-long-password"
CSRF = {"x-ktn-request": "1"}

#: The fixture shelf: bay 1 (SES slot 0) holds sdb, bay 2 (SES slot 1) holds sdc.
ENCLOSURE_DIR = "class/enclosure/1:0:15:0"
SERIAL_IN_SLOT_0 = "K1A00001"
SERIAL_IN_SLOT_1 = "K1A00002"


@pytest.fixture
def sysfs(tmp_path: Path) -> Path:
    root = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, root)
    return root


@pytest.fixture
def client(tmp_path: Path, sysfs: Path) -> Iterator[TestClient]:
    settings = Settings(
        # Never read the repo's .env: tests describe their own world.
        _env_file=None,
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        # Long enough that only the startup poll and the identify route's own
        # refresh ever touch the cache. Staleness here is the subject, not a
        # hazard, so nothing may quietly repair it mid-test.
        poll_slots_seconds=3600.0,
        # The fixture is a synthetic sysfs tree with no real SES device, so pin
        # the sysfs writer, exactly as test_api.py does.
        ident_method="sysfs",
    )
    with TestClient(build_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    client.post("/api/auth/bootstrap",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    client.post("/api/auth/login",
                json={"username": "admin", "password": PASSWORD}, headers=CSRF)
    return client


def _service(client: TestClient) -> object:
    return client.app.state.service


def _latest_audit(client: TestClient) -> dict:
    entries = client.get("/api/audit").json()
    assert entries, "the write was not audited at all"
    return entries[0]


def _bay(client: TestClient, ses_slot: int) -> dict:
    body = client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()
    return next(b for b in body["bays"] if b["ses_slot"] == ses_slot)


def _identify(client: TestClient, ses_slot: int, on: bool, duration: int | None = None) -> object:
    return client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/{ses_slot}/identify",
        json={"on": on, "duration_seconds": duration},
        headers=CSRF,
    )


def _swap_bays(sysfs: Path, first: int, second: int) -> None:
    """Move each bay's block device into the other bay, as a re-seat would.

    Only the bay -> /dev/sdX mapping moves; the devices themselves keep their
    wwid and their VPD serial. That is deliberate - it isolates the half of the
    join that is stale (which bay holds which name, cached for up to
    poll_slots_seconds) from the half that is live (what a given name's disk
    is, re-read and wwid-validated by DiskInfoReader on every call).
    """
    enclosure = sysfs / ENCLOSURE_DIR
    first_block = enclosure / str(first) / "device" / "block"
    second_block = enclosure / str(second) / "device" / "block"
    (first_name,) = [p.name for p in first_block.iterdir()]
    (second_name,) = [p.name for p in second_block.iterdir()]
    (first_block / first_name).rename(first_block / second_name)
    (second_block / second_name).rename(second_block / first_name)


def _empty_bay(sysfs: Path, ses_slot: int) -> None:
    """Pull the drive out of a bay, leaving the slot itself present."""
    block = sysfs / ENCLOSURE_DIR / str(ses_slot) / "device" / "block"
    for child in list(block.iterdir()):
        shutil.rmtree(child)


# ------------------------------------------- #6: the audit serial is permanent


def test_audit_serial_names_the_drive_actually_in_the_bay(
    auth_client: TestClient, sysfs: Path
) -> None:
    """The regression itself: two drives change bays inside one poll interval.

    Before the fix the route read the serial out of ``service.bays()``, whose
    slot rows still said bay 1 held sdb, and wrote sdb's serial into a
    permanent 0600 append-only record for an operation performed on the drive
    that is now sdc. The transient UI glitch repaints; the audit row does not.
    """
    _swap_bays(sysfs, 0, 1)
    assert _bay(auth_client, 0)["disk"]["serial"] == SERIAL_IN_SLOT_0, (
        "precondition: the cached join must still be serving the pre-swap mapping"
    )

    assert _identify(auth_client, 0, on=True, duration=10).status_code == 200

    entry = _latest_audit(auth_client)
    assert entry["ses_slot"] == 0
    assert entry["serial"] == SERIAL_IN_SLOT_1, (
        f"the audit log permanently attributes this write to {entry['serial']!r}, "
        f"but the drive in that bay is {SERIAL_IN_SLOT_1}"
    )


def test_audit_serial_is_omitted_for_a_bay_that_is_now_empty(
    auth_client: TestClient, sysfs: Path
) -> None:
    """Absent beats wrong (§20).

    A drive pulled since the last poll leaves the cached join still naming it.
    Recording that serial claims a drive was in a bay it had already left;
    recording nothing is an established, readable state - the live NAS's own
    system:timer IDENT_OFF rows carry no serial.
    """
    _empty_bay(sysfs, 0)
    assert _bay(auth_client, 0)["disk"]["serial"] == SERIAL_IN_SLOT_0, (
        "precondition: the cached join must still name the removed drive"
    )

    assert _identify(auth_client, 0, on=True, duration=10).status_code == 200

    entry = _latest_audit(auth_client)
    assert entry["ses_slot"] == 0
    assert entry["serial"] is None, (
        f"recorded {entry['serial']!r} for a bay that holds no drive"
    )


def test_audit_serial_is_still_recorded_when_nothing_moved(auth_client: TestClient) -> None:
    """The control. A fix that simply stopped recording serials would satisfy
    both tests above and destroy the field's entire purpose."""
    assert _identify(auth_client, 1, on=True, duration=10).status_code == 200

    entry = _latest_audit(auth_client)
    assert entry["ses_slot"] == 1
    assert entry["bay"] == 2
    assert entry["serial"] == SERIAL_IN_SLOT_1


def test_an_unreadable_shelf_audits_without_a_serial_rather_than_refusing(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving the serial must never become a way for the write to fail.

    A shelf that cannot be re-read is exactly the situation in which somebody
    is pressing Identify. The LED is the operation; the serial is a label on
    the record.
    """
    def unreadable(ref: object) -> list:
        raise OSError("[Errno 5] Input/output error: /sys/class/enclosure")

    monkeypatch.setattr(_service(auth_client).backend, "read_slots", unreadable)

    response = _identify(auth_client, 0, on=True, duration=10)
    assert response.status_code == 200, "a failed identity read blocked the LED write"

    entry = _latest_audit(auth_client)
    assert entry["operation"] == "IDENT_ON"
    assert entry["verification"] == "success"
    assert entry["serial"] is None


def test_audit_serial_survives_enclosure_re_enumeration(
    auth_client: TestClient, sysfs: Path
) -> None:
    """§37: a changed sysfs path must not silently drop the serial from the audit.

    sysfs.py documents ``resolve()`` as required "before every operation so a
    changed /dev/sgX or sysfs path is picked up rather than cached into a
    stale write", and the IDENT write path (SesLocateWriter, via
    ``identify()``) already re-resolves on every call. ``_bay_serial`` instead
    called ``service.backend.read_slots(ref)`` on the ``EnclosureRef`` cached
    by ``service.enclosure()`` - the same one ``service.bays()`` uses, up to
    poll_slots_seconds old. After a re-enumeration (an HBA rescan, a cable
    reseat that lands the shelf on a new SCSI address) that ref still names
    the OLD sysfs path; ``read_slots()`` on a path that no longer exists
    returns no rows at all, and the audit record silently loses the serial -
    precisely when the shelf state is least certain and the record matters
    most.
    """
    assert _bay(auth_client, 0)["disk"]["serial"] == SERIAL_IN_SLOT_0, (
        "precondition: the cache still names the pre-re-enumeration path"
    )

    # The same enclosure (same logical id, from the unchanged 'id' file),
    # discoverable only at a new SCSI address - exactly what re-resolving
    # ahead of a write is meant to pick up.
    old_path = sysfs / ENCLOSURE_DIR
    new_path = sysfs / "class" / "enclosure" / "1:0:16:0"
    old_path.rename(new_path)

    assert _identify(auth_client, 0, on=True, duration=10).status_code == 200

    entry = _latest_audit(auth_client)
    assert entry["ses_slot"] == 0
    assert entry["serial"] == SERIAL_IN_SLOT_0, (
        f"recorded {entry['serial']!r} for a bay whose enclosure had simply "
        "moved to a new sysfs path, not lost its drive"
    )


# ------------------------------ #7: a failing slot cache must be visible on /bays


def test_bays_publishes_the_slot_cache_error_alongside_the_existing_sources(
    auth_client: TestClient,
) -> None:
    """The key must exist unconditionally, or a client cannot tell "no error"
    from "this server does not report errors"."""
    sources = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()["sources"]
    assert sources["slots_error"] is None
    # Additive only: every key the frontend already reads is untouched.
    assert set(sources) >= {"slots", "slots_error", "truenas", "truenas_error", "smart"}
    assert sources["slots"] is not None


def test_a_failed_slot_poll_is_visible_on_bays_not_only_on_diagnostics(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """poll_hardware() catches OSError, calls slots.fail() and keeps the
    last-good rows. That is the right degradation, but it leaves `slots` - the
    last SUCCESS timestamp - frozen at a moment that is receding, and the map
    kept asserting a freshness it no longer had."""
    service = _service(auth_client)
    before = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()
    assert before["sources"]["slots_error"] is None, "control: the cache starts healthy"

    def unreadable(ref: object) -> list:
        raise OSError("[Errno 19] No such device: /sys/class/enclosure/1:0:15:0")

    monkeypatch.setattr(service.backend, "read_slots", unreadable)
    asyncio.run(service.poll_hardware())

    after = auth_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()
    assert "No such device" in (after["sources"]["slots_error"] or ""), (
        "a slot cache that stopped refreshing is invisible to the bay map"
    )
    # §37: the map still degrades gracefully - last-good rows keep being served,
    # they are simply no longer claimed to be current.
    assert after["bays"], "the last-good rows must survive a failed poll"
    assert after["sources"]["slots"] == before["sources"]["slots"], (
        "a failed poll must not advance the last-success timestamp"
    )


def test_the_slot_cache_error_clears_when_polling_recovers(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck error banner is its own lie."""
    service = _service(auth_client)
    real_read_slots = service.backend.read_slots

    def unreadable(ref: object) -> list:
        raise OSError("transient")

    monkeypatch.setattr(service.backend, "read_slots", unreadable)
    asyncio.run(service.poll_hardware())
    assert auth_client.get(
        f"/api/enclosures/{LOGICAL_ID}/bays"
    ).json()["sources"]["slots_error"] is not None

    monkeypatch.setattr(service.backend, "read_slots", real_read_slots)
    asyncio.run(service.poll_hardware())
    assert auth_client.get(
        f"/api/enclosures/{LOGICAL_ID}/bays"
    ).json()["sources"]["slots_error"] is None


# ----------------------- #11: a failed refresh is not a failed write (§26, §37)


def _break_refresh(client: TestClient) -> None:
    """Make the post-write cache refresh raise something poll_hardware does not
    itself swallow. It catches OSError; a shutting-down executor
    ("cannot schedule new futures after shutdown") does not present as one.
    """
    async def broken() -> None:
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    _service(client).poll_hardware = broken  # type: ignore[attr-defined]


def test_a_failed_refresh_does_not_report_a_verified_write_as_a_failure(
    auth_client: TestClient, sysfs: Path
) -> None:
    """By the time the refresh runs the write is done: the hardware read-back
    confirmed it, the record is persisted and the audit line is on disk.
    Answering 500 tells the operator a write that succeeded had failed, so they
    either retry an actuation that already happened or walk to the shelf
    believing an LED is dark while it is lit."""
    _break_refresh(auth_client)

    response = _identify(auth_client, 0, on=True, duration=60)

    assert response.status_code == 200, (
        f"a failed cache refresh turned a verified write into {response.status_code}"
    )
    body = response.json()
    assert body["ok"] is True
    assert body["locate"] is True
    assert body["expires_at"] is not None, "the countdown must survive a failed refresh"
    # The LED really is lit - the assertion above is about the report, not about
    # skipping the hardware write.
    assert (sysfs / ENCLOSURE_DIR / "0" / "locate").read_text().strip() == "1"
    assert _latest_audit(auth_client)["verification"] == "success"


def test_the_response_says_whether_the_cache_was_refreshed(auth_client: TestClient) -> None:
    """Swallowing the failure silently would trade a false 500 for a false 200.
    A caller that renders from this body needs to know which it is holding."""
    ok = _identify(auth_client, 0, on=True, duration=60)
    assert ok.json()["refreshed"] is True

    _break_refresh(auth_client)
    degraded = _identify(auth_client, 0, on=False)
    assert degraded.status_code == 200
    assert degraded.json()["refreshed"] is False


def test_a_lit_led_is_still_clearable_when_the_refresh_fails(auth_client: TestClient) -> None:
    """The consequence the guard exists to prevent, asserted end to end.

    The UI discards the identify response and re-reads /bays, and it disables
    the Clear button on a bay reported dark. So a lit LED reported as dark is a
    stranded LED: a timer is running, the light is on, and the one control that
    would put it out is greyed out. With the refresh broken, /bays is served
    from the pre-write snapshot, and the report must still be the truth.
    """
    _break_refresh(auth_client)
    assert _identify(auth_client, 0, on=True, duration=60).status_code == 200

    bay = _bay(auth_client, 0)
    assert bay["locate"] is True, "a lit LED reported dark disables its own Clear button"
    assert bay["ident_origin"] == "app"
    assert bay["ident_expires_at"] is not None, "no countdown for a running timer"

    # And the clear the operator would then press must actually work.
    assert _identify(auth_client, 0, on=False).status_code == 200
