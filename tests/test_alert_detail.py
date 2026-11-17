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
from pathlib import Path
from typing import Any, Self

import httpx
import pytest
from ktnmgr.config import Settings
from ktnmgr.main import build_app
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

# Captured by running the code at 1549236 (before KTN_ALERT_DETAIL existed)
# and recording httpx.Request(...).content for each message.
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


def _no_serial(health: SlotHealth) -> Bay:
    bay = make_bay(health)
    bay.disk.serial = None
    return bay


def _no_pool(health: SlotHealth) -> Bay:
    """No TrueNAS data: no pool, vdev or errors, and ZFS state UNKNOWN."""
    bay = make_bay(health)
    bay.zfs = ZfsInfo()
    return bay


def _no_temperature_no_device(health: SlotHealth) -> Bay:
    bay = make_bay(health)
    bay.smart.temperature_c = None
    bay.device = None
    bay.zfs.read_errors = bay.zfs.checksum_errors = 0
    return bay


#: More shapes, each alerted on first sight (no "was:" line, previous null).
SHAPES = {
    "no-serial": (_no_serial, SlotHealth.WARNING),
    "no-pool": (_no_pool, SlotHealth.FAILED),
    "no-temperature-no-device": (_no_temperature_no_device, SlotHealth.FAILED),
}

# Captured at 1549236 like the goldens above: (text body, headers, json body).
GOLDEN_SHAPES: dict[str, tuple[bytes, dict[str, str], bytes]] = {
    'no-pool': (
        (
            b'Bay 8 (SES slot 7), serial K1A00008, /dev/sdq, 41C'
        ),
        {'Title': 'Bay 8 is FAILED', 'Priority': 'urgent', 'Tags': 'rotating_light'},
        (
            b'{"title":"Bay 8 is FAILED","message":"Bay 8 (SES slot 7), serial K1A00008, /dev/sdq'
            b', 41C","health":"failed","previous":null,"enclosure":"0x50060480aabbcc00","bay":8,"'
            b'ses_slot":7,"serial":"K1A00008","device":"/dev/sdq","pool":null,"vdev":null,"zfs_st'
            b'ate":"UNKNOWN"}'
        ),
    ),
    'no-serial': (
        (
            b'Bay 8 (SES slot 7), /dev/sdq, tank/raidz3-0, ZFS FAULTED, errors read=3 cksum=12, 4'
            b'1C'
        ),
        {'Title': 'Bay 8 is WARNING', 'Priority': 'high', 'Tags': 'warning'},
        (
            b'{"title":"Bay 8 is WARNING","message":"Bay 8 (SES slot 7), /dev/sdq, tank/raidz3-0,'
            b' ZFS FAULTED, errors read=3 cksum=12, 41C","health":"warning","previous":null,"encl'
            b'osure":"0x50060480aabbcc00","bay":8,"ses_slot":7,"serial":null,"device":"/dev/sdq",'
            b'"pool":"tank","vdev":"raidz3-0","zfs_state":"FAULTED"}'
        ),
    ),
    'no-temperature-no-device': (
        (
            b'Bay 8 (SES slot 7), serial K1A00008, tank/raidz3-0, ZFS FAULTED'
        ),
        {'Title': 'Bay 8 is FAILED', 'Priority': 'urgent', 'Tags': 'rotating_light'},
        (
            b'{"title":"Bay 8 is FAILED","message":"Bay 8 (SES slot 7), serial K1A00008, tank/rai'
            b'dz3-0, ZFS FAULTED","health":"failed","previous":null,"enclosure":"0x50060480aabbcc'
            b'00","bay":8,"ses_slot":7,"serial":"K1A00008","device":null,"pool":"tank","vdev":"ra'
            b'idz3-0","zfs_state":"FAULTED"}'
        ),
    ),
}


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


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit-full"])
async def test_full_is_byte_identical_to_before_for_more_shapes(
    captured: list, shape: str, explicit: bool
) -> None:
    build, health = SHAPES[shape]
    text, headers, body = GOLDEN_SHAPES[shape]
    kwargs = {"detail": "full"} if explicit else {}
    await HealthNotifier(URL, style="ntfy", **kwargs).evaluate([build(health)])
    await HealthNotifier(URL, style="json", **kwargs).evaluate([build(health)])
    assert len(captured) == 2
    assert wire_bytes(captured[0]) == text
    assert captured[0]["headers"] == headers
    assert wire_bytes(captured[1]) == body


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


@pytest.mark.parametrize("raw", ["", " ", "\t"], ids=["empty", "space", "tab"])
def test_an_empty_value_means_the_default(raw: str) -> None:
    """Like KTN_TLS_ENABLED, and like compose's ${KTN_ALERT_DETAIL:-full}.

    `docker run -e KTN_ALERT_DETAIL=` or an empty catalog additional_env
    must not crash-loop the container over an alerting setting.
    """
    assert settings(alert_detail=raw).alert_detail == "full"


def test_an_empty_environment_value_means_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KTN_ALERT_DETAIL", "")
    assert Settings(_env_file=None).alert_detail == "full"


@pytest.mark.parametrize("raw", ["minimial", "none", "redacted", "0", "full minimal"])
def test_setting_refuses_an_unknown_value(raw: str) -> None:
    with pytest.raises(ValidationError, match="KTN_ALERT_DETAIL must be"):
        settings(alert_detail=raw)


def test_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KTN_ALERT_DETAIL", "minimal")
    assert Settings(_env_file=None).alert_detail == "minimal"


# -------------------------------------------------------------------- wiring


@pytest.mark.parametrize("detail", ["minimal", "full"])
def test_build_app_passes_alert_detail_to_the_notifier(tmp_path: Path, detail: str) -> None:
    """The one line in main.build_app that makes the setting do anything.

    Without it the notifier silently keeps its default (full) while the
    operator believes the inventory is hidden; nothing else would go red.
    """
    s = Settings(
        _env_file=None, data_dir=tmp_path / "d", sysfs_root=tmp_path / "s",
        dev_root=tmp_path / "v", ident_method="sysfs",
        alert_webhook_url=URL, alert_detail=detail,
    )
    assert build_app(s).state.service.notifier.detail == detail
