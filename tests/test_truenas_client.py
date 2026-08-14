"""TrueNAS client transport tests.

These cover the behaviour that is easy to get wrong and invisible in normal
operation: that the connection is reused rather than rebuilt per call, that a
dropped socket reconnects once, and that an application-level refusal is not
retried (which would double the failed login attempts against the appliance).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from ktnmgr.truenas.client import TrueNASClient, TrueNASError


class FakeSocket:
    """Minimal stand-in for a websockets connection."""

    def __init__(self, *, fail_on_send_after: int | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._fail_after = fail_on_send_after

    async def send(self, raw: str) -> None:
        if self._fail_after is not None and len(self.sent) >= self._fail_after:
            raise ConnectionError("socket dropped")
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        request = self.sent[-1]
        method = request["method"]
        result = True if method == "auth.login_with_api_key" else [{"name": "tank"}]
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    async def close(self) -> None:
        self.closed = True


def make_client(monkeypatch: pytest.MonkeyPatch, sockets: list[FakeSocket]) -> TrueNASClient:
    client = TrueNASClient("http://truenas.invalid", "key-not-a-real-one")
    opened: list[FakeSocket] = []

    async def fake_connect(url: str, **kwargs: Any) -> FakeSocket:
        socket = sockets[len(opened)]
        opened.append(socket)
        return socket

    monkeypatch.setattr("ktnmgr.truenas.client.websockets.connect", fake_connect)
    client.opened = opened  # type: ignore[attr-defined]
    return client


async def test_connection_is_reused_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """One connect and ONE login, no matter how many calls follow."""
    socket = FakeSocket()
    client = make_client(monkeypatch, [socket])

    for _ in range(5):
        await client.pools()

    assert len(client.opened) == 1, "should not reconnect per call"
    logins = [s for s in socket.sent if s["method"] == "auth.login_with_api_key"]
    assert len(logins) == 1, f"expected exactly one login, got {len(logins)}"


async def test_request_ids_are_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reusing one socket means ids must not repeat, or replies get mismatched."""
    socket = FakeSocket()
    client = make_client(monkeypatch, [socket])
    for _ in range(4):
        await client.pools()
    ids = [s["id"] for s in socket.sent]
    assert len(ids) == len(set(ids))


async def test_dropped_socket_reconnects_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle-timed-out socket must not surface as an error to the caller."""
    dead = FakeSocket(fail_on_send_after=1)  # survives login, dies on the call
    fresh = FakeSocket()
    client = make_client(monkeypatch, [dead, fresh])

    assert await client.pools() == [{"name": "tank"}]
    assert len(client.opened) == 2
    assert dead.closed, "the dead socket should be discarded"


async def test_application_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected key must fail once, not twice - retrying doubles the failed
    login attempts recorded on the appliance."""

    class RejectingSocket(FakeSocket):
        async def recv(self) -> str:
            request = self.sent[-1]
            return json.dumps({
                "jsonrpc": "2.0", "id": request["id"],
                "error": {"message": "Invalid API key"},
            })

    socket = RejectingSocket()
    client = make_client(monkeypatch, [socket])
    with pytest.raises(TrueNASError):
        await client.pools()
    assert len(client.opened) == 1


async def test_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = FakeSocket()
    client = make_client(monkeypatch, [socket])
    await client.pools()
    await client.close()
    await client.close()
    assert socket.closed


async def test_api_key_never_appears_in_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class RejectingSocket(FakeSocket):
        async def recv(self) -> str:
            request = self.sent[-1]
            return json.dumps({
                "jsonrpc": "2.0", "id": request["id"],
                "error": {"message": "Invalid API key"},
            })

    client = make_client(monkeypatch, [RejectingSocket()])
    with pytest.raises(TrueNASError) as excinfo:
        await client.pools()
    assert "key-not-a-real-one" not in str(excinfo.value)
    assert "key-not-a-real-one" not in repr(client)
