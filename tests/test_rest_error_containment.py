"""REST fallback failures must surface as TrueNASError, never as raw httpx/JSON errors.

Why this is its own invariant: httpx transport errors (ConnectError,
TimeoutException, ...) subclass ``httpx.HTTPError``, NOT ``OSError``, and a
200 with a non-JSON body raises ``json.JSONDecodeError`` (a ``ValueError``)
from ``response.json()``. The poll handlers in services/state.py catch exactly
``(TrueNASError, OSError)``, so any of those raw exceptions would escape to
the poll loop's blanket handler - which abandons the whole tick, starving the
unrelated SES/SMART polls at 1 Hz for as long as the NAS stays unreachable.
These tests drive the full ``call()`` path (WebSocket down, fallback opted in)
to pin the containment at the boundary the poll handlers actually see.
"""

from __future__ import annotations

import json
from typing import Any, Self

import httpx
import pytest
from ktnmgr.truenas.client import TrueNASClient, TrueNASError


def break_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(url: str, **kwargs: Any) -> Any:
        raise ConnectionError("no route to host")

    monkeypatch.setattr("ktnmgr.truenas.client.websockets.connect", refuse)


def make_fallback_client() -> TrueNASClient:
    return TrueNASClient("http://truenas.invalid", "key-not-a-real-one", rest_fallback=True)


class FakeAsyncClientBase:
    """Minimal httpx.AsyncClient stand-in; subclasses inject get()."""

    def __init__(self, **kwargs: Any) -> None: ...

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None: ...


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError("all connection attempts failed"),
        httpx.TimeoutException("timed out"),
    ],
    ids=["connect-error", "timeout"],
)
async def test_transport_error_is_contained_as_truenas_error(
    monkeypatch: pytest.MonkeyPatch, transport_error: httpx.HTTPError
) -> None:
    """NAS unreachable with the fallback opted in: TrueNASError, not httpx.*"""
    break_websockets(monkeypatch)

    class RefusingClient(FakeAsyncClientBase):
        async def get(self, url: str, **kwargs: Any) -> Any:
            raise transport_error

    monkeypatch.setattr("ktnmgr.truenas.client.httpx.AsyncClient", RefusingClient)
    client = make_fallback_client()

    # pytest.raises(TrueNASError) is the whole point: were the raw httpx
    # error to escape call(), it would fail this test rather than match.
    with pytest.raises(TrueNASError) as excinfo:
        await client.pools()
    message = str(excinfo.value)
    assert type(transport_error).__name__ in message, (
        "the operator-facing error must name the underlying transport failure"
    )
    assert "key-not-a-real-one" not in message


async def test_non_json_body_is_contained_as_truenas_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with an HTML body (reverse proxy, captive portal) must not leak
    a raw JSONDecodeError past the (TrueNASError, OSError) poll handlers."""
    break_websockets(monkeypatch)

    class HtmlResponse:
        status_code = 200

        def json(self) -> Any:
            return json.loads("<html>Bad Gateway</html>")

    class HtmlClient(FakeAsyncClientBase):
        async def get(self, url: str, **kwargs: Any) -> Any:
            return HtmlResponse()

    monkeypatch.setattr("ktnmgr.truenas.client.httpx.AsyncClient", HtmlClient)
    client = make_fallback_client()

    with pytest.raises(TrueNASError) as excinfo:
        await client.pools()
    assert "non-JSON" in str(excinfo.value)
