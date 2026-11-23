"""The TrueNAS API key is not SENT over http:// or ws:// without an opt-in.

truenas/client.py turns http:// into ws:// and sends the key in
auth.login_with_api_key on every new socket, so over a plain scheme the key
crosses the network unencrypted, and TrueNAS documents that it automatically
revokes a user-linked API key presented over insecure HTTP. So with an
insecure URL, a key, and no KTN_TRUENAS_ALLOW_INSECURE=true:

- the TrueNAS client is not built at all (nothing can send the key);
- one ERROR is logged at startup, naming the scheme and host only (the host
  is withheld when the URL contains "@"), never the key;
- the reason reaches the operator through the surfaces that already report a
  TrueNAS failure: `sources.truenas_error` on /bays (the dashboard's "TrueNAS
  unavailable" banner and the degraded freshness badge) and the TrueNAS
  entries of /api/diagnostics;
- the container keeps running: the drive map, telemetry and Identify work.

With the opt-in, the old behaviour: one WARNING, and the client is built.
https:// and wss:// are unchanged. Every URL and key here is synthetic.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ktnmgr import main as main_module
from ktnmgr.config import Settings, check_truenas_transport
from ktnmgr.main import build_app
from pydantic import ValidationError

KEY = "1-SYNTHETICrefusalKEYneverSENT0123456789"
USERINFO_SECRET = "pw-synthetic-7c1d"
CONFIG_LOGGER = "ktnmgr.config"
FLAG = "KTN_TRUENAS_ALLOW_INSECURE"

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
LOGICAL_ID = "0x50060480aabbcc00"
PASSWORD = "test-fixture-admin-password"
CSRF = {"x-ktn-request": "1"}


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "data_dir": tmp_path / "data",
        "sysfs_root": tmp_path / "sys",
        "dev_root": tmp_path / "dev",
        "ident_method": "sysfs",
        "truenas_api_key": KEY,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == level and r.name == CONFIG_LOGGER
    ]


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# ------------------------------------------------------------- the flag itself


def test_the_opt_in_is_off_by_default(tmp_path: Path) -> None:
    assert settings(tmp_path).truenas_allow_insecure is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("On", True),
        ("false", False), ("0", False), ("no", False), ("OFF", False), ("", False),
    ],
)
def test_the_opt_in_takes_the_same_spellings_as_tls_enabled(
    tmp_path: Path, raw: str, expected: bool
) -> None:
    assert settings(tmp_path, truenas_allow_insecure=raw).truenas_allow_insecure is expected


@pytest.mark.parametrize("raw", ["maybe", "t", "y", "2", "enable", "true!", " true"])
def test_an_unknown_spelling_refuses_to_start(tmp_path: Path, raw: str) -> None:
    """As KTN_TLS_ENABLED: a typo must not silently pick a side."""
    with pytest.raises(ValidationError, match=f"{FLAG} must be one of"):
        settings(tmp_path, truenas_allow_insecure=raw)


def test_the_opt_in_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FLAG, "true")
    assert Settings(_env_file=None).truenas_allow_insecure is True
    monkeypatch.setenv(FLAG, "bogus")
    with pytest.raises(ValidationError, match=f"{FLAG} must be one of"):
        Settings(_env_file=None)


# ----------------------------------------------------------- decision matrix

INSECURE = ["http://nas.example.invalid", "HTTP://nas.example.invalid:80/", "ws://nas.example.invalid"]
SECURE = ["https://nas.example.invalid", "wss://nas.example.invalid"]


@pytest.mark.parametrize("allow", [False, True], ids=["flag-off", "flag-on"])
@pytest.mark.parametrize("key", [KEY, ""], ids=["key", "no-key"])
@pytest.mark.parametrize("url", INSECURE + SECURE)
def test_decision_matrix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str, key: str, allow: bool
) -> None:
    """scheme x key present x flag -> client built?, and what is logged."""
    s = settings(tmp_path, truenas_url=url, truenas_api_key=key, truenas_allow_insecure=allow)
    with caplog.at_level(logging.DEBUG):
        service = build_app(s).state.service
    errors = records(caplog, logging.ERROR)
    warns = records(caplog, logging.WARNING)
    insecure = url.split(":", 1)[0].lower() in {"http", "ws"}

    if not key:
        # No key, nothing to send: "not configured", exactly as before.
        assert service.truenas is None
        assert errors == [] and warns == []
        assert "TrueNAS not configured" in all_log_text(caplog)
    elif insecure and not allow:
        assert service.truenas is None, "the key would be sent in clear"
        assert len(errors) == 1 and warns == []
        assert "TrueNAS not configured" not in all_log_text(caplog)
    elif insecure and allow:
        assert service.truenas is not None, "the opt-in restores today's behaviour"
        assert errors == [] and len(warns) == 1
    else:
        assert service.truenas is not None
        assert errors == [] and warns == []


@pytest.mark.parametrize("url", INSECURE)
def test_check_returns_the_refusal_reason(tmp_path: Path, url: str) -> None:
    reason = check_truenas_transport(settings(tmp_path, truenas_url=url))
    assert isinstance(reason, str) and reason
    assert url.split(":", 1)[0].lower() + "://" in reason
    assert "nas.example.invalid" in reason
    assert f"{FLAG}=true" in reason and "https://" in reason


@pytest.mark.parametrize("url", SECURE + ["", *INSECURE])
def test_check_allows_secure_opted_in_or_unconfigured(tmp_path: Path, url: str) -> None:
    allow = url.split(":", 1)[0].lower() in {"http", "ws"}
    s = settings(tmp_path, truenas_url=url, truenas_allow_insecure=allow)
    assert check_truenas_transport(s) is None


@pytest.mark.parametrize("url", INSECURE)
def test_the_error_names_the_scheme_host_fix_and_vendor_doc(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        check_truenas_transport(settings(tmp_path, truenas_url=url))
    (message,) = records(caplog, logging.ERROR)
    assert url.split(":", 1)[0].lower() + "://" in message
    assert "nas.example.invalid" in message
    assert "KTN_TRUENAS_URL" in message and "https://" in message
    assert f"{FLAG}=true" in message
    assert "revokes" in message and "truenas.com/docs/scale/25.10/api" in message
    # It says what still works, so nobody reads it as "the app is down".
    assert "Identify" in message


# -------------------------------------------------------- no secret anywhere

LEAKY_URLS = [
    f"http://svc:{USERINFO_SECRET}@nas.example.invalid:8080/private/path?token={KEY}",
    f"http://root:pa/{USERINFO_SECRET}@nas.example.invalid/",
    f"http://{USERINFO_SECRET}AbC/dEf+==@nas.example.invalid/",
    f"http://{USERINFO_SECRET}:x?y@nas.example.invalid/",
    f"http://{USERINFO_SECRET}:x#y@nas.example.invalid/",
    f"ws://{USERINFO_SECRET}/tok@nas.example.invalid/",
    f"  http://{USERINFO_SECRET}/tok@nas.example.invalid/",
]


@pytest.mark.parametrize("url", LEAKY_URLS)
def test_the_refusal_never_carries_the_key_or_userinfo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        service = build_app(settings(tmp_path, truenas_url=url)).state.service
    assert service.truenas is None, "control: the refusal path ran"
    errors = records(caplog, logging.ERROR)
    assert len(errors) == 1, "control: the error was logged"
    reason = service.zfs.last_error or ""
    assert reason, "control: the reason was recorded for the UI"
    everything = all_log_text(caplog).lower()
    assert KEY.lower() not in everything
    assert USERINFO_SECRET.lower() not in everything
    # The username and path checks are scoped to what this code formats:
    # other startup lines legitimately contain words like "root".
    for text in (errors[0], reason):
        lowered = text.lower()
        assert "host withheld" in lowered, "control: the userinfo guard ran"
        assert KEY.lower() not in lowered
        assert USERINFO_SECRET.lower() not in lowered
        assert "root" not in lowered and "svc" not in lowered
        assert "/private/path" not in lowered


def test_an_unparsable_host_is_not_echoed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    url = f"http://[{USERINFO_SECRET}"
    with caplog.at_level(logging.DEBUG):
        reason = check_truenas_transport(settings(tmp_path, truenas_url=url))
    assert reason, "control: refused"
    assert records(caplog, logging.ERROR), "control: logged"
    assert USERINFO_SECRET not in all_log_text(caplog)
    assert USERINFO_SECRET not in reason


# ------------------------------------------------------------ startup wiring


def test_no_client_is_constructed_when_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not built-then-discarded: the constructor is never called."""
    calls: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(main_module, "TrueNASClient", spy)
    build_app(settings(tmp_path, truenas_url="http://nas.example.invalid"))
    assert calls == []
    # Control: the same spy does see the opt-in and https builds.
    build_app(settings(tmp_path, truenas_url="http://nas.example.invalid",
                       truenas_allow_insecure=True))
    build_app(settings(tmp_path, truenas_url="https://nas.example.invalid"))
    assert len(calls) == 2


# ------------------------------------------------- what the operator sees


@pytest.fixture
def refused_client(tmp_path: Path) -> Iterator[TestClient]:
    sysfs = tmp_path / "sys"
    shutil.copytree(FIXTURE_ROOT, sysfs)
    s = settings(
        tmp_path,
        sysfs_root=sysfs,
        truenas_url="http://nas.example.invalid",
        poll_slots_seconds=3600.0,
    )
    with TestClient(build_app(s)) as client:
        client.post("/api/auth/bootstrap",
                    json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        client.post("/api/auth/login",
                    json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        yield client


def test_the_banner_source_carries_the_reason(refused_client: TestClient) -> None:
    """`sources.truenas_error` is what App.tsx's "TrueNAS unavailable" notice
    and the freshness badge already render; nothing new for the UI to learn."""
    body = refused_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()
    error = body["sources"]["truenas_error"] or ""
    assert "http://" in error and f"{FLAG}=true" in error
    assert body["sources"]["truenas"] is None
    # The drive map itself is served and fresh.
    assert body["bays"], "the drive map must keep working"
    assert body["sources"]["slots"] is not None
    assert body["sources"]["slots_error"] is None


def test_diagnostics_explains_the_refusal(refused_client: TestClient) -> None:
    diag = refused_client.get("/api/diagnostics").json()
    assert diag["truenas_configured"] is False
    reason = diag["polling"]["truenas"]["last_error"] or ""
    assert f"{FLAG}=true" in reason
    assert diag["truenas_version_unavailable_reason"] == reason
    assert diag["polling"]["smart"]["last_error"] == reason
    assert diag["polling"]["system_info"]["last_error"] == reason


def test_identify_and_health_keep_working(refused_client: TestClient) -> None:
    assert refused_client.get("/healthz").status_code == 200
    response = refused_client.post(
        f"/api/enclosures/{LOGICAL_ID}/slots/0/identify",
        json={"on": True, "duration_seconds": None},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text


def test_the_key_is_in_no_response(refused_client: TestClient) -> None:
    for path in ("/healthz", "/api/diagnostics", f"/api/enclosures/{LOGICAL_ID}/bays",
                 "/api/enclosures", "/api/auth/me"):
        response = refused_client.get(path)
        assert response.status_code in (200, 404), path
        assert KEY not in response.text, path


def test_reason_survives_the_poll_loop(refused_client: TestClient) -> None:
    """poll_truenas returns early with no client; it must not clear the reason."""
    import asyncio

    service = refused_client.app.state.service  # type: ignore[attr-defined]
    asyncio.run(service.poll_truenas())
    asyncio.run(service.poll_smart())
    asyncio.run(service.poll_system_info())
    body = refused_client.get(f"/api/enclosures/{LOGICAL_ID}/bays").json()
    assert f"{FLAG}=true" in (body["sources"]["truenas_error"] or "")
