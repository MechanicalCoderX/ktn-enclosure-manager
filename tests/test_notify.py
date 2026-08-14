"""Health notification tests.

The properties that matter: it fires on transitions only (a permanently
degraded pool must not message every poll), it survives restarts without
re-announcing, and a broken notification endpoint never disturbs polling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import httpx
import pytest
from ktnmgr.models import Bay, SlotHealth, ZfsInfo
from ktnmgr.services.notify import HealthNotifier


def make_bay(slot: int, health: SlotHealth, **kwargs: Any) -> Bay:
    return Bay(
        display_bay=slot + 1, ses_slot=slot, enclosure_id="0x50060480aabbcc00",
        device=f"/dev/sd{chr(98 + slot)}", health=health, status="OK",
        zfs=ZfsInfo(pool="tank", vdev="raidz3-0", state="ONLINE"), **kwargs,
    )


class FakeResponse:
    """Minimal stand-in. Delivery is judged by status code, so it needs one."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            sent.append({"url": url, **kwargs})
            return FakeResponse(200)

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", FakeClient)
    return sent


async def test_disabled_without_a_url(captured: list) -> None:
    notifier = HealthNotifier(url="")
    assert not notifier.enabled
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert captured == []


async def test_notifies_on_degradation(captured: list, tmp_path: Path) -> None:
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")
    await notifier.evaluate([make_bay(0, SlotHealth.OK)])
    assert captured == [], "healthy bays must not notify"

    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(captured) == 1
    assert "urgent" == captured[0]["headers"]["Priority"]
    assert "Bay 1" in captured[0]["headers"]["Title"]


async def test_steady_state_does_not_repeat(captured: list, tmp_path: Path) -> None:
    """A permanently degraded pool must not message on every poll."""
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")
    for _ in range(10):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(captured) == 1


async def test_recovery_is_announced_once(captured: list, tmp_path: Path) -> None:
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    await notifier.evaluate([make_bay(0, SlotHealth.OK)])
    await notifier.evaluate([make_bay(0, SlotHealth.OK)])
    assert len(captured) == 2
    assert "recovered" in captured[1]["headers"]["Title"]


async def test_recovery_can_be_suppressed(captured: list, tmp_path: Path) -> None:
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json",
                              notify_recovery=False)
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    await notifier.evaluate([make_bay(0, SlotHealth.OK)])
    assert len(captured) == 1


async def test_state_survives_a_restart(captured: list, tmp_path: Path) -> None:
    """Restarting must not re-announce what the operator already knows."""
    state = tmp_path / "n.json"
    first = HealthNotifier("http://ntfy/topic", state_path=state)
    await first.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(captured) == 1

    second = HealthNotifier("http://ntfy/topic", state_path=state)
    await second.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(captured) == 1, "a restart re-announced an existing condition"


async def test_first_observation_of_a_failure_does_notify(
    captured: list, tmp_path: Path
) -> None:
    """A drive already dead when the app starts is exactly what to report."""
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")
    await notifier.evaluate([make_bay(3, SlotHealth.FAILED)])
    assert len(captured) == 1


async def test_json_style_payload(captured: list, tmp_path: Path) -> None:
    notifier = HealthNotifier("http://hook/x", style="json", state_path=tmp_path / "n.json")
    await notifier.evaluate([make_bay(2, SlotHealth.WARNING)])
    payload = captured[0]["json"]
    assert payload["health"] == "warning"
    assert payload["bay"] == 3
    assert payload["pool"] == "tank"


async def test_delivery_failure_never_breaks_polling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BrokenClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, *a: Any, **k: Any) -> None:
            raise httpx.ConnectError("nope")

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", BrokenClient)
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # must not raise


async def test_message_is_actionable(captured: list, tmp_path: Path) -> None:
    """The message has to say which drive to pull."""
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")
    bay = make_bay(7, SlotHealth.FAILED)
    bay.disk.serial = "K1A00008"
    await notifier.evaluate([bay])
    body = captured[0]["content"].decode()
    assert "Bay 8" in body and "SES slot 7" in body
    assert "K1A00008" in body
    assert "tank/raidz3-0" in body


async def test_a_rejected_post_is_not_reported_as_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A mistyped ntfy topic answers 404. Logging "notified" in that case is
    the worst failure mode an alerting path can have: it looks healthy."""

    class RejectingClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, *a: Any, **k: Any) -> FakeResponse:
            return FakeResponse(404)

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", RejectingClient)
    notifier = HealthNotifier("http://ntfy/wrong-topic", state_path=tmp_path / "n.json")

    with caplog.at_level("INFO", logger="ktnmgr.services.notify"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    messages = [r.getMessage() for r in caplog.records]
    assert not any(m.startswith("notified:") for m in messages), messages
    assert any("rejected" in m and "404" in m for m in messages), messages


async def test_many_simultaneous_changes_are_sent_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Losing TrueNAS changes every bay at once. Serial delivery against a slow
    endpoint stalled the poll loop for bays x timeout."""
    import asyncio

    concurrent = 0
    peak = 0

    class SlowClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, *a: Any, **k: Any) -> FakeResponse:
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1
            return FakeResponse(200)

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", SlowClient)
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")

    bays = [make_bay(i, SlotHealth.FAILED) for i in range(15)]
    started = asyncio.get_running_loop().time()
    await notifier.evaluate(bays)
    elapsed = asyncio.get_running_loop().time() - started

    assert peak > 1, "notifications were delivered one at a time"
    assert elapsed < 15 * 0.05, f"delivery was serialised ({elapsed:.2f}s)"
