"""The legacy REST fallback is opt-in, and its errors tell the truth.

Two measured facts drive this (both on TrueNAS 25.10.5): a role-scoped key is
refused wholesale by REST - 403 on every read that works over JSON-RPC - and
the REST surface is removed entirely in 26.04. So on the recommended key the
fallback could never succeed; it could only convert a transient WebSocket blip
into a false "TrueNAS rejected the API key" alarm, which is precisely the
message that sends an operator off to rotate a key that was fine.
"""

from __future__ import annotations

from typing import Any, Self

import pytest
from ktnmgr.truenas.client import TrueNASClient, TrueNASError


def break_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(url: str, **kwargs: Any) -> Any:
        raise ConnectionError("no route to host")

    monkeypatch.setattr("ktnmgr.truenas.client.websockets.connect", refuse)


async def test_fallback_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WebSocket failure surfaces as a transport error, not a REST attempt."""
    break_websockets(monkeypatch)
    client = TrueNASClient("http://truenas.invalid", "key-not-a-real-one")

    rest_calls: list[str] = []

    async def record_rest(method: str, params: list[Any]) -> Any:
        rest_calls.append(method)
        return []

    monkeypatch.setattr(client, "_call_rest", record_rest)

    with pytest.raises(TrueNASError) as excinfo:
        await client.pools()
    assert rest_calls == [], "REST must not be attempted unless opted in"
    message = str(excinfo.value)
    assert "REST fallback is disabled" in message
    assert "rejected the API key" not in message, (
        "a transport failure must never read like a credential failure"
    )


async def test_fallback_runs_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    break_websockets(monkeypatch)
    client = TrueNASClient(
        "http://truenas.invalid", "key-not-a-real-one", rest_fallback=True
    )

    async def fake_rest(method: str, params: list[Any]) -> Any:
        return [{"name": "tank"}]

    monkeypatch.setattr(client, "_call_rest", fake_rest)
    assert await client.pools() == [{"name": "tank"}]


async def test_rest_403_is_not_reported_as_a_bad_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 means the key lacks REST roles; 401 means the key is bad.

    Conflating them is what sent an operator to rotate a working key.
    """

    class FakeResponse:
        status_code = 403

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> Self:
            return self
        async def __aexit__(self, *exc: object) -> None: ...
        async def get(self, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("ktnmgr.truenas.client.httpx.AsyncClient", FakeAsyncClient)
    client = TrueNASClient(
        "http://truenas.invalid", "key-not-a-real-one", rest_fallback=True
    )

    with pytest.raises(TrueNASError) as excinfo:
        await client._call_rest("pool.query", [])
    message = str(excinfo.value)
    assert "does not mean the key is bad" in message
    assert message != "TrueNAS rejected the API key"
