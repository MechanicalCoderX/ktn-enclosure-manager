"""A TrueNAS API key configured with a plain http:// URL is warned about.

truenas/client.py turns http:// into ws:// and sends the key in
auth.login_with_api_key on every new socket, so with an http:// URL the key
crosses the network unencrypted. That stays allowed (refusing it would break
a deployment that works today), but it is no longer silent: one WARNING at
startup naming the scheme and host. The key, any userinfo and the path never
appear in it. Every URL and key here is synthetic.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from ktnmgr.config import Settings, check_truenas_transport
from ktnmgr.main import build_app

KEY = "1-SYNTHETICapikeyNEVERlogged0123456789"
USERINFO_SECRET = "pw-synthetic-9f3e"
CONFIG_LOGGER = "ktnmgr.config"


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


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and r.name == CONFIG_LOGGER
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://nas.example.invalid",
        "http://nas.example.invalid:80/",
        "HTTP://nas.example.invalid",
        "ws://nas.example.invalid",
    ],
)
def test_plain_scheme_with_a_key_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert check_truenas_transport(settings(tmp_path, truenas_url=url)) is True
    found = warnings(caplog)
    assert len(found) == 1
    message = found[0]
    assert "nas.example.invalid" in message
    assert url.split(":", 1)[0].lower() + "://" in message
    assert "KTN_TRUENAS_URL" in message and "https://" in message


@pytest.mark.parametrize("url", ["https://nas.example.invalid", "wss://nas.example.invalid"])
def test_encrypted_scheme_is_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert check_truenas_transport(settings(tmp_path, truenas_url=url)) is False
    assert warnings(caplog) == []


@pytest.mark.parametrize(
    ("url", "key"),
    [("", KEY), ("http://nas.example.invalid", "")],
    ids=["no-url", "no-key"],
)
def test_nothing_to_warn_about_when_truenas_is_not_configured(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str, key: str
) -> None:
    """No client is built without both, so no key is ever sent."""
    with caplog.at_level(logging.DEBUG):
        assert check_truenas_transport(
            settings(tmp_path, truenas_url=url, truenas_api_key=key)
        ) is False
    assert warnings(caplog) == []


def test_the_key_and_userinfo_never_reach_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    url = f"http://svc:{USERINFO_SECRET}@nas.example.invalid:8080/private/path?token={KEY}"
    with caplog.at_level(logging.DEBUG):
        assert check_truenas_transport(settings(tmp_path, truenas_url=url)) is True
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert caplog.records, "control: nothing was logged, so this proves nothing"
    assert "nas.example.invalid" in text, "control: the host should be named"
    assert KEY not in text
    assert USERINFO_SECRET not in text
    assert "svc" not in text
    assert "/private/path" not in text


def test_an_unparsable_host_is_not_echoed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """urlsplit refuses an unbalanced '['; the warning must still be safe."""
    url = f"http://[{USERINFO_SECRET}"
    with caplog.at_level(logging.DEBUG):
        assert check_truenas_transport(settings(tmp_path, truenas_url=url)) is True
    text = "\n".join(warnings(caplog))
    assert text, "control: the warning was not logged"
    assert USERINFO_SECRET not in text


def test_build_app_warns_exactly_once_and_never_logs_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The real startup path, not just the helper."""
    with caplog.at_level(logging.DEBUG):
        build_app(settings(tmp_path, truenas_url="http://nas.example.invalid"))
    assert len(warnings(caplog)) == 1
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert KEY not in everything


def test_build_app_is_silent_on_https(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        build_app(settings(tmp_path, truenas_url="https://nas.example.invalid"))
    assert warnings(caplog) == []
