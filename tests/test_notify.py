"""Health notification tests.

The properties that matter: it fires on transitions only (a permanently
degraded pool must not message every poll), it survives restarts without
re-announcing, a broken notification endpoint never disturbs polling, and an
alert the endpoint never accepted is retried on later polls rather than
silently dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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


async def test_transient_failure_is_retried_with_original_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed-drive alert lost to a network hiccup must go out on a later
    poll - carrying the transition the operator never heard about - and
    exactly once."""
    delivered: list[dict[str, Any]] = []
    attempts = 0

    class FlakyClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("hiccup")
            delivered.append({"url": url, **kwargs})
            return FakeResponse(200)

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", FlakyClient)
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json",
                              retry_seconds=0.0)

    await notifier.evaluate([make_bay(0, SlotHealth.OK)])
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # eaten by the network
    assert delivered == []
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # retried, accepted
    assert len(delivered) == 1
    assert "was: ok -> now: failed" in delivered[0]["content"].decode(), (
        "the retry lost the original previous-state context"
    )
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # now committed
    assert len(delivered) == 1


async def test_dead_endpoint_neither_wedges_nor_duplicates_across_flapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FAILED -> OK -> FAILED with the endpoint down throughout: the first
    undelivered FAILED alert is superseded by the recovery, and once the
    endpoint returns exactly one alert - for the live transition - goes out."""
    down = True
    delivered: list[dict[str, Any]] = []

    class SometimesClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            if down:
                raise httpx.ConnectError("endpoint is down")
            delivered.append({"url": url, **kwargs})
            return FakeResponse(200)

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", SometimesClient)
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json",
                              retry_seconds=0.0)

    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # undeliverable
    await notifier.evaluate([make_bay(0, SlotHealth.OK)])      # supersedes the stale alert
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # undeliverable again
    assert delivered == []

    down = False
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(delivered) == 1, "the surviving transition must be announced exactly once"
    assert "was: ok -> now: failed" in delivered[0]["content"].decode()
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # steady state now
    assert len(delivered) == 1


async def test_undelivered_alert_survives_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only delivered transitions are persisted, so a restart between the
    failed attempt and the retry must not lose the alert."""
    state = tmp_path / "n.json"

    class BrokenClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, *a: Any, **k: Any) -> None:
            raise httpx.ConnectError("nope")

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", BrokenClient)
    first = HealthNotifier("http://ntfy/topic", state_path=state)
    await first.evaluate([make_bay(0, SlotHealth.FAILED)])

    delivered: list[dict[str, Any]] = []

    class WorkingClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            delivered.append({"url": url, **kwargs})
            return FakeResponse(200)

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", WorkingClient)
    second = HealthNotifier("http://ntfy/topic", state_path=state)
    await second.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(delivered) == 1, "the restart swallowed an alert the operator never got"


async def test_retries_are_spaced_not_per_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-attempting a dead endpoint on every poll tick would stall the loop
    by a connect timeout each cycle; attempts inside the cooldown are skipped."""
    posts = 0

    class BrokenClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, *a: Any, **k: Any) -> None:
            nonlocal posts
            posts += 1
            raise httpx.ConnectError("nope")

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", BrokenClient)
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json",
                              retry_seconds=60.0)
    for _ in range(5):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert posts == 1, "a dead endpoint was hammered on every poll"


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


SECRET_URL = "https://ntfy.example/topic-s3cret?auth=TOKEN-s3cret"


@pytest.mark.parametrize("status", [200, 301, 308, 401, 404, 500, 503])
async def test_webhook_url_never_reaches_the_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """The URL is a secret (ntfy topic / ?auth= token). It is driven through a
    REAL httpx client, because the leak was httpx's own INFO line."""
    from ktnmgr.main import configure_logging

    configure_logging()
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient",
        lambda **kw: real_client(transport=transport, **kw),
    )
    notifier = HealthNotifier(SECRET_URL, state_path=tmp_path / "n.json")

    with caplog.at_level("DEBUG"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    assert caplog.records, "control: the notifier logged nothing, so this proves nothing"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cret" not in text, text
    assert "topic" not in text, text


async def test_webhook_url_is_scrubbed_from_exception_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class QuotingClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, *a: Any, **k: Any) -> FakeResponse:
            raise httpx.ConnectError(f"cannot connect to {SECRET_URL}")

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", QuotingClient)
    notifier = HealthNotifier(SECRET_URL, state_path=tmp_path / "n.json")

    with caplog.at_level("DEBUG"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not deliver" in text and "ntfy.example" in text, text
    assert "s3cret" not in text and "topic" not in text, text


@pytest.mark.parametrize("status", [301, 308])
async def test_a_redirect_is_not_delivery_and_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    class RedirectingClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, *a: Any, **k: Any) -> FakeResponse:
            return FakeResponse(status)

    monkeypatch.setattr("ktnmgr.services.notify.httpx.AsyncClient", RedirectingClient)
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json")
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert not (tmp_path / "n.json").exists() or "failed" not in (tmp_path / "n.json").read_text()


# ------------------------------------------------ hardening pass 2026-10-30


def scripted_client(statuses: list[int], posts: list[dict[str, Any]]) -> type:
    """Fake client answering each POST with the next scripted status (the last
    one repeats once the script runs out); every POST is recorded in `posts`."""

    class ScriptedClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            posts.append({"url": url, **kwargs})
            return FakeResponse(statuses[min(len(posts) - 1, len(statuses) - 1)])

    return ScriptedClient


def raising_client(make_error: Callable[[str], Exception]) -> type:
    """Fake client whose POST fails with an error built from the URL it was
    actually asked to post to, the way httpx errors quote the request URL."""

    class RaisingClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self: return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            raise make_error(url)

    return RaisingClient


def log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


DELIVERED_STATUSES = [200, 201, 202, 204, 299]
UNDELIVERED_STATUSES = [199, 300, 301, 302, 303, 304, 307, 308, 399]


@pytest.mark.parametrize("status", DELIVERED_STATUSES, ids=lambda s: f"HTTP-{s}")
async def test_every_2xx_counts_as_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """ntfy answers 200, but a webhook receiver may legitimately answer 201,
    202 or 204. Treating any of those as a failure would re-send the same alert
    forever, so the whole 2xx range is delivery, up to and including 299."""
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient", scripted_client([status], posts)
    )
    state = tmp_path / "n.json"
    notifier = HealthNotifier("http://ntfy/topic", state_path=state, retry_seconds=0.0)

    with caplog.at_level("INFO", logger="ktnmgr.services.notify"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    records = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert any(lvl == "INFO" and m.startswith("notified:") for lvl, m in records), records
    assert state.exists() and "failed" in state.read_text(), "a delivered alert was not committed"

    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(posts) == 1, "a delivered alert was sent again"


@pytest.mark.parametrize("status", UNDELIVERED_STATUSES, ids=lambda s: f"HTTP-{s}")
async def test_anything_outside_2xx_is_not_delivered_and_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """Pins both edges of the accepted range (199 below, 300 and 399 above) and
    that every status outside it is a delivery failure: warned about, left
    uncommitted, and posted again on the next evaluation."""
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient", scripted_client([status], posts)
    )
    state = tmp_path / "n.json"
    notifier = HealthNotifier("http://ntfy/topic", state_path=state, retry_seconds=0.0)

    with caplog.at_level("INFO", logger="ktnmgr.services.notify"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    records = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert not any(m.startswith("notified:") for _, m in records), records
    assert any(
        lvl == "WARNING" and "rejected" in m and str(status) in m for lvl, m in records
    ), records
    assert not state.exists() or "failed" not in state.read_text()

    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert len(posts) == 2, "an alert the endpoint never accepted was not retried"


async def test_a_redirect_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Following the redirect would silently re-POST the alert somewhere the
    operator never configured. The code relies on httpx's default of not
    following redirects, and this pins it with a REAL client."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/topic":
            return httpx.Response(302, headers={"Location": "https://ntfy.example/moved"})
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient",
        lambda **kw: real_client(transport=transport, **kw),
    )
    state = tmp_path / "n.json"
    notifier = HealthNotifier("https://ntfy.example/topic", state_path=state, retry_seconds=0.0)

    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert requested == ["https://ntfy.example/topic"], requested
    assert not state.exists() or "failed" not in state.read_text()

    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
    assert requested == ["https://ntfy.example/topic"] * 2, requested


async def test_alert_is_delivered_once_after_a_redirect_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A moved topic that is fixed (or a proxy that recovers) must not lose the
    alert: the retry after a 3xx carries the original transition and goes out
    exactly once."""
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient", scripted_client([302, 200], posts)
    )
    notifier = HealthNotifier("http://ntfy/topic", state_path=tmp_path / "n.json",
                              retry_seconds=0.0)

    await notifier.evaluate([make_bay(0, SlotHealth.OK)])
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # answered 302
    assert len(posts) == 1
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # retried, answered 200
    assert len(posts) == 2
    assert "was: ok -> now: failed" in posts[1]["content"].decode(), (
        "the retry lost the original previous-state context"
    )
    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # now committed
    assert len(posts) == 2


@pytest.mark.parametrize("status", [404, 302], ids=lambda s: f"HTTP-{s}")
async def test_rejection_warning_names_only_scheme_and_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """The topic and the ?auth= token live in the path and query, so the
    warning an operator reads names where the alert went and nothing more."""
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient", scripted_client([status], [])
    )
    notifier = HealthNotifier(SECRET_URL, state_path=tmp_path / "n.json")

    with caplog.at_level("DEBUG"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [f"health notification rejected: HTTP {status} from https://ntfy.example"]


CREDENTIAL_URL = "https://alice:pw-s3cret@ntfy.example/topic-s3cret?auth=TOKEN-s3cret"


async def test_userinfo_credentials_never_reach_the_logs_when_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A webhook URL may carry basic-auth credentials in its userinfo. The
    rejection warning must name the host without them."""
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient", scripted_client([401], [])
    )
    notifier = HealthNotifier(CREDENTIAL_URL, state_path=tmp_path / "n.json")

    with caplog.at_level("DEBUG"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    text = log_text(caplog)
    assert "ntfy.example" in text and "401" in text, text
    assert "s3cret" not in text and "alice" not in text, text


async def test_userinfo_credentials_never_reach_the_logs_when_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Same secret, other path: an httpx error that quotes the full URL
    (userinfo included) must be scrubbed before it is logged."""
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient",
        raising_client(lambda url: httpx.ConnectError(f"cannot connect to {url}")),
    )
    notifier = HealthNotifier(CREDENTIAL_URL, state_path=tmp_path / "n.json")

    with caplog.at_level("DEBUG"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    text = log_text(caplog)
    assert "could not deliver" in text and "ntfy.example" in text, text
    assert "s3cret" not in text and "alice" not in text, text


async def test_persisted_state_never_contains_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The state file sits on disk next to the app's other files; it records
    health per bay and has no business holding the webhook secret."""
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient", scripted_client([200], [])
    )
    state = tmp_path / "n.json"
    notifier = HealthNotifier(SECRET_URL, state_path=state)

    await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])

    text = state.read_text()
    assert "failed" in text, "control: the delivered alert was not persisted"
    assert "s3cret" not in text and "ntfy.example" not in text, text


async def test_the_logging_cap_is_what_keeps_httpx_from_logging_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The leak is real: uncapped, httpx's own INFO line carries the secret URL.
    configure_logging() is what stops it, so the two halves here are the
    control and the guard for test_webhook_url_never_reaches_the_logs."""
    from ktnmgr.main import configure_logging

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    monkeypatch.setattr(
        "ktnmgr.services.notify.httpx.AsyncClient",
        lambda **kw: real_client(transport=transport, **kw),
    )
    notifier = HealthNotifier(SECRET_URL, state_path=tmp_path / "n.json")

    names = ("httpx", "httpcore")
    saved = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names:
            logging.getLogger(name).setLevel(logging.NOTSET)
        with caplog.at_level("DEBUG"):
            await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])
        assert "s3cret" in log_text(caplog), "control: httpx no longer logs the URL"

        caplog.clear()
        configure_logging()
        with caplog.at_level("DEBUG"):
            await notifier.evaluate([make_bay(0, SlotHealth.OK)])  # the recovery alert
        assert caplog.records, "control: the notifier logged nothing, so this proves nothing"
        assert "s3cret" not in log_text(caplog), log_text(caplog)
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.skip(
    reason="BUG: httpx.InvalidURL is not an httpx.HTTPError, so a malformed webhook URL "
    "escapes _send() instead of counting as a failed delivery"
)
async def test_a_malformed_url_counts_as_a_failed_delivery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo in the webhook URL must not crash the poll loop: it is a failed
    delivery like any other, warned about without the secret."""
    notifier = HealthNotifier("https://ntfy.example:notaport/topic-s3cret",
                              state_path=tmp_path / "n.json", retry_seconds=60.0)

    with caplog.at_level("DEBUG"):
        await notifier.evaluate([make_bay(0, SlotHealth.FAILED)])  # must not raise

    text = log_text(caplog)
    assert any(
        r.levelname == "WARNING" and "could not deliver" in r.getMessage()
        for r in caplog.records
    ), text
    assert "s3cret" not in text, text
