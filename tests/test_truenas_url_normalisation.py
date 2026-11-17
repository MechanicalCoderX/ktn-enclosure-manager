"""KTN_TRUENAS_URL: one scheme rule for the check and the client; blank is unset.

N1 (REVIEW-164). check_truenas_transport() reads the scheme case-insensitively
(urlsplit lower-cases it), as RFC 3986 says schemes are. The client compared
it as text, so an upper-case spelling the check allowed then misbehaved:
``HTTPS://`` never connected (websockets refused the un-mapped URI),
``WSS://`` silently ignored KTN_TRUENAS_VERIFY_TLS and KTN_TRUENAS_CA_BUNDLE
(no TLS context was built, so websockets used its default one), and an
opted-in ``HTTP://`` never connected. None of it sent the key in clear; all of
it was wrong. The client now normalises the scheme once, so every spelling
behaves exactly like its lower-case form.

N2 (REVIEW-164). A KTN_TRUENAS_URL of only whitespace passed main.py's "is a
URL set" test, so a client was built for a URL that could never connect, with
neither a refusal nor the "TrueNAS not configured" warning. Blank now means
not configured everywhere.

Every URL and key here is synthetic.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any

import pytest
from ktnmgr import main as main_module
from ktnmgr.config import Settings, check_truenas_transport
from ktnmgr.main import build_app
from ktnmgr.truenas import client as client_module
from ktnmgr.truenas.client import TrueNASClient, normalise_url
from websockets.uri import parse_uri

KEY = "1-SYNTHETICnormaliseKEY0123456789"
HOST = "nas.example.invalid"
CA_BUNDLE = "/synthetic/ca-bundle.pem"


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


# ------------------------------------------------------------- N1: the scheme

#: (spelling, its lower-case form). Every non-lower-case spelling of the four
#: schemes the client handles, in the shapes an operator might type.
SPELLINGS = [
    (f"HTTPS://{HOST}", f"https://{HOST}"),
    (f"Https://{HOST}:8443/", f"https://{HOST}:8443/"),
    (f"WSS://{HOST}", f"wss://{HOST}"),
    (f"wSs://{HOST}:8443", f"wss://{HOST}:8443"),
    (f"HTTP://{HOST}", f"http://{HOST}"),
    (f"hTTp://{HOST}:80/", f"http://{HOST}:80/"),
    (f"WS://{HOST}", f"ws://{HOST}"),
    (f"Ws://{HOST}", f"ws://{HOST}"),
]
SECURE_SCHEMES = {"https", "wss"}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("HTTPS://NAS.Example.invalid:8443/Sub/", "https://NAS.Example.invalid:8443/Sub"),
        (f"WSS://{HOST}", f"wss://{HOST}"),
        (f"  https://{HOST}/  ", f"https://{HOST}"),
        (f"https://{HOST}", f"https://{HOST}"),
        # Not a scheme: nothing before "://" may be touched.
        (f"{HOST}/Path?next=HTTP://Other", f"{HOST}/Path?next=HTTP://Other"),
        (f"ht tp://{HOST}", f"ht tp://{HOST}"),
        ("", ""),
    ],
)
def test_only_the_scheme_is_lower_cased(given: str, expected: str) -> None:
    assert normalise_url(given) == expected
    assert TrueNASClient(url=given, api_key=KEY).url == expected


def describe_tls(client: TrueNASClient, monkeypatch: pytest.MonkeyPatch) -> object:
    """What _ssl_context() builds: False, or the context's settings and how it was made."""
    made: list[dict[str, Any]] = []
    real = ssl.create_default_context

    def recording(**kwargs: Any) -> ssl.SSLContext:
        made.append(kwargs)
        return real()  # never reads the (synthetic) CA file

    monkeypatch.setattr(client_module.ssl, "create_default_context", recording)
    context = client._ssl_context()
    monkeypatch.setattr(client_module.ssl, "create_default_context", real)
    if context is False:
        return False
    assert isinstance(context, ssl.SSLContext)
    return (made, context.verify_mode, context.check_hostname)


@pytest.mark.parametrize(
    "tls", [{}, {"verify_tls": False}, {"ca_bundle": CA_BUNDLE}],
    ids=["verify", "no-verify", "ca-bundle"],
)
@pytest.mark.parametrize(("given", "lower"), SPELLINGS)
def test_every_spelling_behaves_like_its_lower_case_form(
    monkeypatch: pytest.MonkeyPatch, given: str, lower: str, tls: dict[str, Any]
) -> None:
    """Same URL, same WebSocket URL, same TLS context, same REST base."""
    odd = TrueNASClient(url=given, api_key=KEY, **tls)
    plain = TrueNASClient(url=lower, api_key=KEY, **tls)

    assert odd.url == plain.url  # the REST fallback's base
    assert odd._ws_url == plain._ws_url
    assert describe_tls(odd, monkeypatch) == describe_tls(plain, monkeypatch)

    # And the lower-case form does what it should, so "the same" means right.
    secure = lower.split(":", 1)[0] in SECURE_SCHEMES
    uri = parse_uri(odd._ws_url)  # raised InvalidURI for HTTPS:// and HTTP://
    assert uri.secure is secure
    assert uri.host == HOST
    tls_desc = describe_tls(odd, monkeypatch)
    if not secure:
        assert tls_desc is False
    else:
        assert tls_desc is not False, "no TLS context: verify_tls / ca_bundle ignored"
        made, verify_mode, check_hostname = tls_desc  # type: ignore[misc]
        if tls.get("verify_tls") is False:
            assert (verify_mode, check_hostname) == (ssl.CERT_NONE, False)
        else:
            assert (verify_mode, check_hostname) == (ssl.CERT_REQUIRED, True)
            assert made == [{"cafile": CA_BUNDLE} if "ca_bundle" in tls else {}]


@pytest.mark.parametrize(("given", "lower"), SPELLINGS)
def test_the_client_agrees_with_the_transport_check(
    tmp_path: Path, given: str, lower: str
) -> None:
    """Where the check says TLS, the client uses TLS; where it says plain, plain.

    With the opt-in on, so the check lets every spelling through and the
    client is built for each.
    """
    s = settings(tmp_path, truenas_url=given, truenas_allow_insecure=True)
    assert check_truenas_transport(s) is None
    secure = lower.split(":", 1)[0] in SECURE_SCHEMES
    client = build_app(s).state.service.truenas
    assert isinstance(client, TrueNASClient)
    assert parse_uri(client._ws_url).secure is secure


def test_the_websocket_mapping_rewrites_only_the_scheme() -> None:
    client = TrueNASClient(url=f"HTTPS://{HOST}/via/http://x", api_key=KEY)
    assert client._ws_url == f"wss://{HOST}/via/http://x/api/current"


# --------------------------------------------------- N2: blank means unset

BLANK = ["   ", "\t", " \n ", ""]


@pytest.mark.parametrize("blank", BLANK, ids=repr)
def test_a_blank_url_is_not_configured(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, blank: str
) -> None:
    s = settings(tmp_path, truenas_url=blank)
    assert s.truenas_url == ""
    with caplog.at_level(logging.DEBUG):
        service = build_app(s).state.service
    assert service.truenas is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("TrueNAS not configured" in m for m in messages)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    diag = service.diagnostics()
    assert diag["truenas_configured"] is False
    assert diag["truenas_url"] is None


def test_no_client_is_constructed_for_a_blank_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(main_module, "TrueNASClient", spy)
    build_app(settings(tmp_path, truenas_url="   "))
    assert calls == []
    # Control: the same spy sees a real URL, passed on without its padding.
    build_app(settings(tmp_path, truenas_url=f"  https://{HOST}  "))
    assert [c["url"] for c in calls] == [f"https://{HOST}"]


def test_a_blank_url_from_the_environment_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KTN_TRUENAS_URL", "   ")
    assert Settings(_env_file=None).truenas_url == ""
    monkeypatch.setenv("KTN_TRUENAS_URL", f" https://{HOST} ")
    assert Settings(_env_file=None).truenas_url == f"https://{HOST}"
