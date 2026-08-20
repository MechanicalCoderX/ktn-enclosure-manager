"""truenas_version must survive the least-privilege key.

``system.info`` accepts only READONLY_ADMIN/SHARING_ADMIN, so on the
recommended role-scoped key it is always denied - and diagnostics showed
``truenas_version: null`` forever, tempting an operator to hold a broader key
just to display a version string. ``system.version`` carries no authorization
requirement in the middleware, so the one field the UI uses is recoverable on
any key. These tests pin the fallback and that it does not mask real outages.
"""

from __future__ import annotations

from typing import Any

import pytest
from ktnmgr.services.state import StateService
from ktnmgr.truenas.client import TrueNASClient, TrueNASError


def make_service(truenas: Any) -> StateService:
    # poll_system_info touches only .truenas and .system_info; the other
    # collaborators are stored, never called, in this path.
    return StateService(
        settings=None, backend=None, disks=None, ses=None, ident=None,
        truenas=truenas, notifier=None,
    )


class DeniedInfo:
    """A least-privilege key: system.info refused, system.version fine."""

    async def system_info(self) -> dict[str, Any]:
        raise TrueNASError("system.info failed: Not authorized")

    async def version(self) -> str:
        return "TrueNAS-25.10.6"


class AllDown:
    """A real outage: nothing answers."""

    async def system_info(self) -> dict[str, Any]:
        raise TrueNASError("system.info failed: Not authorized")

    async def version(self) -> str:
        raise TrueNASError("transport down")


async def test_version_is_recovered_on_a_narrow_key() -> None:
    service = make_service(DeniedInfo())
    await service.poll_system_info()
    assert service.system_info.value == {"version": "TrueNAS-25.10.6"}
    assert service.system_info.last_error is None


async def test_a_real_outage_still_reports_the_original_error() -> None:
    """The fallback must not convert an outage into a silent empty success."""
    service = make_service(AllDown())
    await service.poll_system_info()
    assert service.system_info.value == {}
    assert "Not authorized" in (service.system_info.last_error or "")


async def test_client_version_is_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "system.version" in TrueNASClient.ALLOWED_METHODS

    client = TrueNASClient("http://truenas.invalid", "key-not-a-real-one")

    async def fake_ws(method: str, params: list[Any]) -> Any:
        assert method == "system.version"
        return "TrueNAS-25.10.6"

    monkeypatch.setattr(client, "_call_ws", fake_ws)
    assert await client.version() == "TrueNAS-25.10.6"
