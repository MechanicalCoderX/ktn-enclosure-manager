"""TrueNAS over-temperature alerts as a bay health signal.

`disk.temperature_alerts` was allow-listed, wrapped, and never called - and the
wrapper omitted the `names` argument the appliance requires, so it would have
failed with `[EINVAL] names: Field required` had anything used it. These tests
pin both the argument and the meaning.
"""

from __future__ import annotations

from typing import Any

import pytest
from ktnmgr.models import SlotHealth, SmartInfo, ZfsInfo, ZfsState
from ktnmgr.services.state import classify
from ktnmgr.truenas.correlate import build_smart_index


def alert_for(device: str, text: str = "Disk sdf is too hot") -> dict[str, Any]:
    """Shaped like a real alert.list entry of class DiskTemperatureTooHot."""
    return {
        "klass": "DiskTemperatureTooHot",
        "args": {"device": f"/dev/{device}"},
        "formatted": text,
        "level": "WARNING",
    }


def test_alert_marks_the_right_disk() -> None:
    index = build_smart_index(
        {"sdb": 30.0, "sdf": 61.0},
        alerts=[alert_for("sdf", "Disk sdf temperature is 61C")],
    )

    assert index["sdf"].over_temperature is True
    assert index["sdf"].alert == "Disk sdf temperature is 61C"
    assert index["sdb"].over_temperature is False
    assert index["sdb"].alert is None


def test_no_alerts_leaves_every_disk_clear() -> None:
    index = build_smart_index({"sdb": 30.0, "sdf": 31.0}, alerts=[])
    assert all(not entry.over_temperature for entry in index.values())


def test_alert_for_an_unknown_disk_still_lands() -> None:
    """A disk absent from the temperature map must not swallow its own alert."""
    index = build_smart_index({}, alerts=[alert_for("sdz")])
    assert index["sdz"].over_temperature is True


def test_malformed_alert_is_ignored_not_fatal() -> None:
    index = build_smart_index(
        {"sdb": 30.0},
        alerts=[{}, {"args": {}}, {"args": {"device": ""}}, alert_for("sdb")],
    )
    assert index["sdb"].over_temperature is True


def test_over_temperature_is_a_warning() -> None:
    hot = SmartInfo(temperature_c=61.0, available=True, over_temperature=True)
    assert classify("OK", False, True, ZfsInfo(), hot) is SlotHealth.WARNING


def test_a_faulted_hot_disk_still_reads_as_failed() -> None:
    """Temperature must not downgrade a real fault."""
    hot = SmartInfo(temperature_c=61.0, available=True, over_temperature=True)
    faulted = ZfsInfo(state=ZfsState.FAULTED)
    assert classify("OK", False, True, faulted, hot) is SlotHealth.FAILED


def test_health_is_unchanged_when_smart_is_absent() -> None:
    assert classify("OK", False, True, ZfsInfo(), None) is SlotHealth.OK
    assert classify("OK", False, True, ZfsInfo()) is SlotHealth.OK


@pytest.mark.asyncio
async def test_client_passes_the_required_names_argument() -> None:
    from ktnmgr.truenas.client import TrueNASClient

    client = TrueNASClient(url="http://nas.invalid", api_key="k")
    seen: list[tuple[str, list[Any]]] = []

    async def fake_call(method: str, params: list[Any] | None = None) -> Any:
        seen.append((method, params or []))
        return []

    client.call = fake_call  # type: ignore[method-assign]

    await client.temperature_alerts(["sdb", "sdf"])
    assert seen == [("disk.temperature_alerts", [["sdb", "sdf"]])]


@pytest.mark.asyncio
async def test_empty_name_list_does_not_call_the_appliance() -> None:
    from ktnmgr.truenas.client import TrueNASClient

    client = TrueNASClient(url="http://nas.invalid", api_key="k")
    called = False

    async def fake_call(method: str, params: list[Any] | None = None) -> Any:
        nonlocal called
        called = True
        return []

    client.call = fake_call  # type: ignore[method-assign]

    assert await client.temperature_alerts([]) == []
    assert called is False


@pytest.mark.asyncio
async def test_alerts_unavailable_does_not_break_the_smart_poll() -> None:
    """There is no REST fallback for this method, so a WebSocket cooldown must
    degrade to 'no alerts' rather than failing the whole poll."""
    from ktnmgr.truenas.client import TrueNASClient, TrueNASError

    client = TrueNASClient(url="http://nas.invalid", api_key="k")

    async def failing_call(method: str, params: list[Any] | None = None) -> Any:
        raise TrueNASError("disk.temperature_alerts has no REST fallback")

    client.call = failing_call  # type: ignore[method-assign]

    assert await client.temperature_alerts(["sdb"]) == []
