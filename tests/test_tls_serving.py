"""The app served over TLS by uvicorn itself, as the container runs it.

These start a real ``python -m uvicorn`` - TLS needs a real listener; the
TestClient never negotiates any. Each behaviour is checked against a control
so a broken harness cannot pass: plain HTTP is refused on the TLS port while
the same request to a plain instance succeeds, and the session cookie is
Secure over HTTPS while the same login over HTTP is not.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from tls_certs import HOSTNAME, make_pair, run_server

PASSWORD = "correct horse battery"  # throwaway test account


def verified_context(cafile: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cafile))


def request(
    port: int,
    path: str,
    *,
    context: ssl.SSLContext | None = None,
    method: str = "GET",
    body: dict | None = None,
    host: str = HOSTNAME,
) -> tuple[int, http.client.HTTPMessage, bytes]:
    """One request. TLS when a context is given, with SNI and hostname check."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    if context is not None:
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        conn.sock = context.wrap_socket(raw, server_hostname=HOSTNAME)
    headers = {"Host": f"{host}:{port}", "X-KTN-Request": "1"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[int, Path]]:
    """TLS instance laid out like the container: mounted source, tmpfs copy."""
    tmp = tmp_path_factory.mktemp("tls")
    cert, key = make_pair(tmp / "mounted")
    runtime = tmp / "run-ktn-tls"
    runtime.mkdir()
    served_cert, served_key = runtime / "cert.pem", runtime / "key.pem"
    served_cert.write_bytes(cert.read_bytes())
    served_key.write_bytes(key.read_bytes())
    env = {
        "KTN_TLS_ENABLED": "true",
        "KTN_TLS_CERTFILE": str(cert),
        "KTN_TLS_KEYFILE": str(key),
        "KTN_TLS_RUNTIME_DIR": str(runtime),
        "KTN_ALLOWED_HOSTS": HOSTNAME,
    }
    with run_server(tmp, cert=served_cert, key=served_key, env=env) as port:
        yield port, cert


@pytest.fixture(scope="module")
def plain_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """The control: a default install, TLS off, no new settings at all."""
    with run_server(tmp_path_factory.mktemp("plain")) as port:
        yield port


def test_https_healthz_is_200_with_a_verified_certificate(tls_server: tuple[int, Path]) -> None:
    port, cert = tls_server
    status, _, body = request(port, "/healthz", context=verified_context(cert))
    assert status == 200
    tls = json.loads(body)["tls"]
    assert tls["enabled"] is True
    assert tls["days_left"] in (29, 30)
    assert tls["reload_pending"] is False


def test_plain_healthz_has_no_tls_block(plain_server: int) -> None:
    status, _, body = request(plain_server, "/healthz")
    assert status == 200
    assert "tls" not in json.loads(body)


def test_plain_http_on_the_tls_port_fails(tls_server: tuple[int, Path], plain_server: int) -> None:
    port, _ = tls_server
    with pytest.raises((ConnectionError, http.client.HTTPException)):
        request(port, "/healthz")
    # Control: the identical plain request to a plain instance succeeds.
    assert request(plain_server, "/healthz")[0] == 200


def login_cookie(port: int, context: ssl.SSLContext | None) -> str:
    status, _, _ = request(
        port, "/api/auth/bootstrap", context=context, method="POST",
        body={"username": "admin", "password": PASSWORD},
    )
    assert status == 200
    status, headers, _ = request(
        port, "/api/auth/login", context=context, method="POST",
        body={"username": "admin", "password": PASSWORD},
    )
    assert status == 200
    cookie = headers.get("set-cookie")
    assert cookie and cookie.startswith("ktn_session=")
    return cookie


def test_the_session_cookie_is_secure_over_https_only(
    tls_server: tuple[int, Path], plain_server: int
) -> None:
    port, cert = tls_server
    secure = login_cookie(port, verified_context(cert))
    assert "secure" in [part.strip().lower() for part in secure.split(";")]
    # Control: the same login over plain HTTP is not marked Secure.
    plain = login_cookie(plain_server, None)
    assert "secure" not in [part.strip().lower() for part in plain.split(";")]


def test_a_foreign_host_is_refused(tls_server: tuple[int, Path]) -> None:
    port, cert = tls_server
    status, _, _ = request(port, "/healthz", context=verified_context(cert), host="evil.example")
    assert status == 400
    # Control: the configured name is answered on the same connection setup.
    assert request(port, "/healthz", context=verified_context(cert))[0] == 200


def legacy_client(maximum: ssl.TLSVersion) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # SECLEVEL=0 so it is the SERVER refusing TLS 1.1, not this client's own
    # OpenSSL declining to offer it.
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        context.minimum_version = ssl.TLSVersion.TLSv1_1
        context.maximum_version = maximum
    return context


def legacy_server_negotiates(tmp_path: Path) -> str | None:
    """Control: this client CAN speak TLS 1.1 to a server that allows it."""
    cert, key = make_pair(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.set_ciphers("DEFAULT:@SECLEVEL=0")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        server_context.minimum_version = ssl.TLSVersion.TLSv1_1
        server_context.maximum_version = ssl.TLSVersion.TLSv1_1
    server_context.load_cert_chain(str(cert), str(key))
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        try:
            with server_context.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        with legacy_client(ssl.TLSVersion.TLSv1_1).wrap_socket(raw) as tls:
            return tls.version()
    finally:
        thread.join(timeout=10)
        listener.close()


def test_a_tls_1_1_client_is_refused(tls_server: tuple[int, Path], tmp_path: Path) -> None:
    # Control first: without it, a client unable to offer TLS 1.1 at all would
    # "be refused" by any server and the assertion below would prove nothing.
    assert legacy_server_negotiates(tmp_path) == "TLSv1.1"
    port, _ = tls_server
    with pytest.raises(ssl.SSLError) as refused:
        request(port, "/healthz", context=legacy_client(ssl.TLSVersion.TLSv1_1))
    # The refusal must come from the server's version floor. A client that
    # could not even build a TLS 1.1 hello would fail with this instead, and
    # the test would prove nothing.
    assert refused.value.reason != "NO_PROTOCOLS_AVAILABLE", refused.value
    # Control: the same permissive client capped at TLS 1.2 is served.
    assert request(port, "/healthz", context=legacy_client(ssl.TLSVersion.TLSv1_2))[0] == 200
