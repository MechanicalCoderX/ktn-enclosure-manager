"""KTN_ALERT_DETAIL: how much drive inventory a health alert carries.

The alert goes to whatever host KTN_ALERT_WEBHOOK_URL names, which can be a
public ntfy.sh topic: retained by that server and readable by anyone who
knows (or guesses) the topic. ``full`` (the default) is the message as it has
always been - bay, serial, /dev/sdX, pool/vdev, ZFS state, error counts,
temperature, and in the JSON style the enclosure logical id. ``minimal``
keeps the bay number and the condition, and drops everything that inventories
the machine: serial, device path, pool and vdev names, enclosure id.

The golden values below were captured from the code at 1549236, before the
setting existed, so "full is unchanged" is checked against the old bytes, not
against itself. Every URL here is synthetic.
"""

from __future__ import annotations

import json
from typing import Any, Self

import httpx
import pytest
from ktnmgr.config import Settings
from ktnmgr.models import Bay, SlotHealth, ZfsInfo
from ktnmgr.services.notify import HealthNotifier
from pydantic import ValidationError

URL = "http://ntfy.example.invalid/synthetic-topic"

SERIAL = "K1A00008"
DEVICE = "/dev/sdq"
POOL = "tank"
VDEV = "raidz3-0"
ENCLOSURE = "0x50060480aabbcc00"
#: Everything minimal must never send. "SES slot 7" is listed as a string
#: because the text style names the slot that way.
IDENTIFYING = (SERIAL, DEVICE, POOL, VDEV, ENCLOSURE, "SES slot")

# Captured at 1549236 (scratch-L5L6/golden.py): httpx.Request(...).content.
GOLDEN_TEXT = {
    "failed": (
        b"Bay 8 (SES slot 7), serial K1A00008, /dev/sdq, tank/raidz3-0, ZFS FAULTED, "
        b"errors read=3 cksum=12, 41C\nwas: ok -> now: failed"
    ),
    "ok": (
        b"Bay 8 (SES slot 7), serial K1A00008, /dev/sdq, tank/raidz3-0, ZFS FAULTED, "
        b"errors read=3 cksum=12, 41C\nwas: failed -> now: ok"
    ),
}
GOLDEN_HEADERS = {
    "failed": {"Title": "Bay 8 is FAILED", "Priority": "urgent", "Tags": "rotating_light"},
    "ok": {"Title": "Bay 8 recovered", "Priority": "default", "Tags": "white_check_mark"},
}
GOLDEN_JSON = {
    "failed": (
        b'{"title":"Bay 8 is FAILED","message":"Bay 8 (SES slot 7), serial K1A00008, '
        b"/dev/sdq, tank/raidz3-0, ZFS FAULTED, errors read=3 cksum=12, 41C\\nwas: ok -> "
        b'now: failed","health":"failed","previous":"ok","enclosure":"0x50060480aabbcc00",'
        b'"bay":8,"ses_slot":7,"serial":"K1A00008","device":"/dev/sdq","pool":"tank",'
        b'"vdev":"raidz3-0","zfs_state":"FAULTED"}'
    ),
    "ok": (
        b'{"title":"Bay 8 recovered","message":"Bay 8 (SES slot 7), serial K1A00008, '
        b"/dev/sdq, tank/raidz3-0, ZFS FAULTED, errors read=3 cksum=12, 41C\\nwas: failed "
        b'-> now: ok","health":"ok","previous":"failed","enclosure":"0x50060480aabbcc00",'
        b'"bay":8,"ses_slot":7,"serial":"K1A00008","device":"/dev/sdq","pool":"tank",'
        b'"vdev":"raidz3-0","zfs_state":"FAULTED"}'
    ),
}


def make_bay(health: SlotHealth) -> Bay:
    bay = Bay(
        display_bay=8, ses_slot=7, enclosure_id=ENCLOSURE, device=DEVICE,
        health=health, status="OK",
        zfs=ZfsInfo(pool=POOL, vdev=VDEV, state="FAULTED",
                    read_errors=3, write_errors=0, checksum_errors=12),
    )
    bay.disk.serial = SERIAL
    bay.smart.temperature_c = 41.4
    return bay


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    class Response:
        status_code = 200

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, url: str, **kwargs: Any) -> Response:
            sent.append(kwargs)
            return Response()

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", FakeClient)
    return sent


def wire_bytes(kwargs: dict[str, Any]) -> bytes:
    """The body exactly as httpx would put it on the wire."""
    return httpx.Request("POST", URL, **kwargs).content


async def fail_then_recover(notifier: HealthNotifier) -> None:
    await notifier.evaluate([make_bay(SlotHealth.OK)])      # first sight: silent
    await notifier.evaluate([make_bay(SlotHealth.FAILED)])  # alert
    await notifier.evaluate([make_bay(SlotHealth.OK)])      # recovery alert


# ------------------------------------------------------------- full = today


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit-full"])
async def test_full_text_is_byte_identical_to_before(
    captured: list, explicit: bool
) -> None:
    kwargs = {"detail": "full"} if explicit else {}
    await fail_then_recover(HealthNotifier(URL, style="ntfy", **kwargs))
    assert len(captured) == 2
    for sent, which in zip(captured, ("failed", "ok"), strict=True):
        assert wire_bytes(sent) == GOLDEN_TEXT[which]
        assert sent["headers"] == GOLDEN_HEADERS[which]


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit-full"])
async def test_full_json_is_byte_identical_to_before(
    captured: list, explicit: bool
) -> None:
    kwargs = {"detail": "full"} if explicit else {}
    await fail_then_recover(HealthNotifier(URL, style="json", **kwargs))
    assert len(captured) == 2
    for sent, which in zip(captured, ("failed", "ok"), strict=True):
        assert wire_bytes(sent) == GOLDEN_JSON[which]


# ------------------------------------------------------------------ minimal


async def test_minimal_text_names_the_bay_and_condition_only(captured: list) -> None:
    await fail_then_recover(HealthNotifier(URL, style="ntfy", detail="minimal"))
    assert len(captured) == 2
    failed, recovered = (wire_bytes(s).decode() for s in captured)
    assert failed == (
        "Bay 8, ZFS FAULTED, errors read=3 cksum=12, 41C\nwas: ok -> now: failed"
    )
    assert recovered == (
        "Bay 8, ZFS FAULTED, errors read=3 cksum=12, 41C\nwas: failed -> now: ok"
    )
    # Headers carry no inventory in either mode; they are unchanged.
    assert captured[0]["headers"] == GOLDEN_HEADERS["failed"]
    assert captured[1]["headers"] == GOLDEN_HEADERS["ok"]
    for sent in captured:
        everything = wire_bytes(sent).decode() + repr(sent["headers"])
        for value in IDENTIFYING:
            assert value not in everything, f"minimal text leaked {value!r}"


async def test_minimal_json_keeps_the_keys_and_nulls_the_inventory(
    captured: list,
) -> None:
    await fail_then_recover(HealthNotifier(URL, style="json", detail="minimal"))
    assert len(captured) == 2
    payload = captured[0]["json"]
    # Same keys as full, so a consumer's parser keeps working; the
    # identifying ones are null (serial and enclosure could already be).
    assert set(payload) == set(json.loads(GOLDEN_JSON["failed"]))
    assert payload["bay"] == 8
    assert payload["health"] == "failed"
    assert payload["previous"] == "ok"
    assert payload["zfs_state"] == "FAULTED"
    for key in ("enclosure", "ses_slot", "serial", "device", "pool", "vdev"):
        assert payload[key] is None, key
    for sent in captured:
        body = wire_bytes(sent).decode()
        for value in IDENTIFYING:
            assert value not in body, f"minimal json leaked {value!r}"


async def test_minimal_with_nothing_but_a_bay(captured: list) -> None:
    """A bay with no disk data at all still produces a sensible message."""
    bay = Bay(display_bay=3, ses_slot=2, enclosure_id=ENCLOSURE, health=SlotHealth.FAILED,
              status="Critical")
    await HealthNotifier(URL, detail="minimal").evaluate([bay])
    assert wire_bytes(captured[0]) == b"Bay 3"


async def test_an_unknown_detail_value_fails_closed_to_minimal(captured: list) -> None:
    """Constructed directly with a typo, the notifier sends less, not more.

    Settings refuses the typo at startup (below); this is the second line,
    for any caller that bypasses Settings.
    """
    notifier = HealthNotifier(URL, detail="minimial")
    assert notifier.detail == "minimal"
    await notifier.evaluate([make_bay(SlotHealth.FAILED)])
    body = wire_bytes(captured[0]).decode()
    for value in IDENTIFYING:
        assert value not in body


# ------------------------------------------------------------------ settings


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_setting_defaults_to_full() -> None:
    assert settings().alert_detail == "full"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("full", "full"), ("minimal", "minimal"), ("MINIMAL", "minimal"), (" Full ", "full")],
)
def test_setting_accepts_both_values_in_any_case(raw: str, expected: str) -> None:
    assert settings(alert_detail=raw).alert_detail == expected


@pytest.mark.parametrize("raw", ["minimial", "none", "", "redacted", "0"])
def test_setting_refuses_an_unknown_value(raw: str) -> None:
    with pytest.raises(ValidationError, match="KTN_ALERT_DETAIL must be"):
        settings(alert_detail=raw)


def test_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KTN_ALERT_DETAIL", "minimal")
    assert Settings(_env_file=None).alert_detail == "minimal"
