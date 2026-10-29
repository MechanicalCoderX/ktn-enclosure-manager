"""KTN_TLS_* settings: off by default, strict when on, one spelling table.

The spelling table is shared by the app (config.py) and the entrypoint
(scripts/tls-bool.sh). The parity test runs the real shell function over the
same inputs, so the two cannot drift: if they did, the entrypoint could start
uvicorn without TLS while the app believed it was on.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from ktnmgr.config import Settings
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
TLS_BOOL = ROOT / "scripts" / "tls-bool.sh"

ON = {"tls_enabled": True, "tls_certfile": "/tls/fullchain.pem", "tls_keyfile": "/tls/privkey.pem"}


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_off_by_default() -> None:
    s = settings()
    assert s.tls_enabled is False
    assert s.tls_certfile is None and s.tls_keyfile is None
    assert s.tls_reload_check_seconds == 3600
    assert s.tls_served_certfile == Path("/run/ktn/tls/cert.pem")
    assert s.tls_served_keyfile == Path("/run/ktn/tls/key.pem")


def test_enabled_with_both_paths_is_accepted() -> None:
    s = settings(**ON)
    assert s.tls_enabled is True
    assert s.tls_certfile == Path("/tls/fullchain.pem")


@pytest.mark.parametrize("missing", ["tls_certfile", "tls_keyfile"])
def test_enabled_without_a_path_raises(missing: str) -> None:
    values = {k: v for k, v in ON.items() if k != missing}
    with pytest.raises(ValidationError, match="is not set"):
        settings(**values)


def test_a_relative_path_raises() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        settings(**{**ON, "tls_keyfile": "tls/privkey.pem"})


def test_cert_and_key_must_differ() -> None:
    with pytest.raises(ValidationError, match="different"):
        settings(**{**ON, "tls_keyfile": "/tls/./fullchain.pem"})


def test_paths_are_not_checked_while_tls_is_off() -> None:
    """A plain install that carries stray values is unchanged."""
    s = settings(tls_certfile="relative.pem", tls_keyfile="relative.pem")
    assert s.tls_enabled is False


def test_zero_disables_the_watcher_and_30_is_rejected() -> None:
    assert settings(**ON, tls_reload_check_seconds=0).tls_reload_check_seconds == 0
    assert settings(**ON, tls_reload_check_seconds=60).tls_reload_check_seconds == 60
    with pytest.raises(ValidationError, match="at least 60"):
        settings(**ON, tls_reload_check_seconds=30)
    with pytest.raises(ValidationError, match="at least 60"):
        settings(**ON, tls_reload_check_seconds=-1)


def test_settle_has_a_floor() -> None:
    assert settings(tls_reload_settle_seconds=5).tls_reload_settle_seconds == 5
    with pytest.raises(ValidationError, match="at least 5"):
        settings(tls_reload_settle_seconds=4)


def test_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KTN_TLS_ENABLED", "yes")
    monkeypatch.setenv("KTN_TLS_CERTFILE", "/tls/fullchain.pem")
    monkeypatch.setenv("KTN_TLS_KEYFILE", "/tls/privkey.pem")
    monkeypatch.setenv("KTN_TLS_RUNTIME_DIR", "/elsewhere")
    s = Settings(_env_file=None)
    assert s.tls_enabled is True
    assert s.tls_served_certfile == Path("/elsewhere/cert.pem")


# Every row is (input, expected): True/False, or None for "refused".
SPELLINGS = [
    ("1", True), ("true", True), ("TRUE", True), ("True", True), ("yes", True),
    ("YES", True), ("on", True), ("On", True),
    ("0", False), ("false", False), ("FALSE", False), ("no", False), ("No", False),
    ("off", False), ("OFF", False), ("", False),
    # Refused by both. Several of these pydantic's own bool would accept
    # (t, y, f, n) - the narrower table is what keeps the shell in step.
    ("t", None), ("y", None), ("f", None), ("n", None), ("2", None), ("-1", None),
    ("enabled", None), (" true", None), ("true ", None), ("true\n", None),
    ("tru", None), ("onn", None), ("ｔrue", None), ("yes please", None),
]


def python_verdict(value: str) -> bool | None:
    try:
        return Settings(
            _env_file=None, tls_enabled=value,
            tls_certfile="/tls/fullchain.pem", tls_keyfile="/tls/privkey.pem",
        ).tls_enabled
    except ValidationError:
        return None


def shell_verdict(value: str) -> bool | None:
    assert shutil.which("sh")
    result = subprocess.run(
        ["sh", "-c", '. "$1"; ktn_bool "$2"', "sh", str(TLS_BOOL), value],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0:
        assert result.stdout == ""
        return None
    return {"true\n": True, "false\n": False}[result.stdout]


@pytest.mark.parametrize(("value", "expected"), SPELLINGS)
def test_python_and_shell_agree_on_every_spelling(value: str, expected: bool | None) -> None:
    assert python_verdict(value) is expected
    assert shell_verdict(value) is expected


def test_the_parity_table_is_not_vacuous() -> None:
    """Control: both sides really do return all three outcomes."""
    outcomes = {expected for _, expected in SPELLINGS}
    assert outcomes == {True, False, None}
    assert shell_verdict("on") is True and shell_verdict("off") is False
    assert shell_verdict("maybe") is None
