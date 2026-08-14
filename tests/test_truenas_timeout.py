"""A TrueNAS connection that never answers must not stall polling forever.

`recv()` has no timeout of its own. A socket that is open but silent - a
half-open connection after a network partition, or an appliance mid-restart -
would block inside the client lock indefinitely, freezing every TrueNAS poll
for the life of the process and leaving the UI showing stale pool data with no
error to explain it.
"""

from __future__ import annotations

import asyncio

import pytest
from ktnmgr.truenas.client import TrueNASClient


class SilentSocket:
    """Accepts the request, then never replies."""

    def __init__(self) -> None:
        self.closed = False

    async def send(self, _payload: str) -> None:
        return None

    async def recv(self) -> str:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class ChattySocket:
    """Replies, but only with messages for other request ids."""

    def __init__(self, replies: int = 10_000) -> None:
        self.replies = replies
        self.closed = False

    async def send(self, _payload: str) -> None:
        return None

    async def recv(self) -> str:
        await asyncio.sleep(0)
        return '{"jsonrpc":"2.0","id":999999,"result":null}'

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_a_silent_socket_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TrueNASClient(url="http://nas.invalid", api_key="k", timeout=0.2)

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(
            client._ws_request(SilentSocket(), 1, "system.info", []),
            timeout=5,
        )


@pytest.mark.asyncio
async def test_endless_unrelated_traffic_still_times_out() -> None:
    """The id-mismatch loop must be bounded by the same deadline."""
    client = TrueNASClient(url="http://nas.invalid", api_key="k", timeout=0.3)

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(
            client._ws_request(ChattySocket(), 1, "system.info", []),
            timeout=5,
        )


@pytest.mark.asyncio
async def test_a_matching_reply_is_returned() -> None:
    class Answering:
        async def send(self, _payload: str) -> None:
            return None

        async def recv(self) -> str:
            return '{"jsonrpc":"2.0","id":7,"result":{"version":"25.10.5"}}'

        async def close(self) -> None:
            return None

    client = TrueNASClient(url="http://nas.invalid", api_key="k", timeout=5)
    result = await client._ws_request(Answering(), 7, "system.info", [])
    assert result == {"version": "25.10.5"}
