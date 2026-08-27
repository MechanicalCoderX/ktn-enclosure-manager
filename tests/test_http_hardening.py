"""HTTP hardening: limiter thread-safety, Host validation, 429s, proxy trust.

Four related pins from the v1.5.4 review:

* The login rate limiter is shared mutable state reached from FastAPI's
  threadpool (the auth routes are plain ``def``), so its check must hold under
  genuinely parallel callers, not just sequential tests.
* ``KTN_ALLOWED_HOSTS`` is the DNS-rebinding defence for the opt-in anonymous
  modes; unset it must change nothing, set it must refuse a foreign Host.
* The limiter refusal is 429 on login as well as on change-password - the two
  endpoints share one limiter and must not describe the same refusal as
  "wrong credentials" on one and "slow down" on the other.
* ``KTN_FORWARDED_ALLOW_IPS`` reaches uvicorn, because the Secure cookie flag
  derives from the request scheme and the scheme behind a TLS proxy is only
  correct when uvicorn believes that proxy's X-Forwarded-Proto.
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from ktnmgr.config import Settings
from ktnmgr.main import build_app
from ktnmgr.services.auth import AuthError, RateLimiter

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sysfs_root"
ENTRYPOINT = Path(__file__).parent.parent / "docker-entrypoint.sh"
PASSWORD = "a-sufficiently-long-password"
CSRF = {"x-ktn-request": "1"}


def make_client(tmp_path: Path, **overrides) -> TestClient:
    """A TestClient over the standard synthetic sysfs fixture.

    A factory rather than a fixture because the Host-validation tests need two
    differently-configured apps in one test module.
    """
    sysfs = tmp_path / "sys"
    if not sysfs.exists():
        shutil.copytree(FIXTURE_ROOT, sysfs)
    settings = Settings(
        _env_file=None,  # never read the repo's .env: tests describe their own world
        sysfs_root=sysfs,
        dev_root=tmp_path / "dev",
        data_dir=tmp_path / "data",
        truenas_url="",
        ident_method="sysfs",
        **overrides,
    )
    return TestClient(build_app(settings))


# ------------------------------------------------------- limiter thread-safety


def test_rate_limiter_holds_under_parallel_callers() -> None:
    """Exactly ``limit`` concurrent attempts get through, never more.

    The auth routes are sync ``def``, so FastAPI runs them on a threadpool of
    ~40 workers and ``check()`` is a read-modify-write on a shared dict.
    Before the lock, parallel callers could each read the same "4 attempts"
    list, all conclude there was room, and all be admitted - a burst beat the
    limit, which is the one control between a password and a brute force. The
    barrier releases every thread at once to make that interleaving as likely
    as the scheduler allows; with the lock the outcome is exact, not merely
    probable.
    """
    limit = 5
    threads = 24
    limiter = RateLimiter(limit=limit, window_seconds=60)
    barrier = threading.Barrier(threads)

    def attempt() -> bool:
        barrier.wait()
        try:
            limiter.check("10.0.0.99")
        except AuthError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=threads) as pool:
        admitted = sum(pool.map(lambda _: attempt(), range(threads)))
    assert admitted == limit


# ------------------------------------------------------------ Host validation


def test_unset_allowed_hosts_accepts_any_host(tmp_path: Path) -> None:
    """The default is allow-all, so existing deployments keep working.

    Every install today is reached by whatever IP or hostname the operator
    chose; a default that guessed at it would brick them all on upgrade.
    """
    with make_client(tmp_path) as client:
        assert client.get("/healthz", headers={"host": "anything.example"}).status_code == 200


def test_allowed_hosts_refuses_a_foreign_host(tmp_path: Path) -> None:
    """A rebound hostname still carries the attacker's Host header - refuse it,
    and refuse it on the API surface as well as the health endpoint."""
    with make_client(tmp_path, allowed_hosts="nas.example") as client:
        assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 400
        assert client.get("/api/auth/status",
                          headers={"host": "evil.example"}).status_code == 400


def test_allowed_hosts_always_answers_loopback(tmp_path: Path) -> None:
    """The image's own HEALTHCHECK probes http://127.0.0.1:8420/healthz, so a
    hardened container would report permanently unhealthy if the allow-list
    could exclude loopback. It cannot: a rebound request's Host names the
    attacker's domain, never loopback, so the standing exemption costs the
    rebinding defence nothing."""
    with make_client(tmp_path, allowed_hosts="nas.example") as client:
        assert client.get("/healthz", headers={"host": "127.0.0.1:8420"}).status_code == 200
        assert client.get("/healthz", headers={"host": "localhost"}).status_code == 200
        assert client.get("/healthz", headers={"host": "[::1]:8420"}).status_code == 200


def test_allowed_hosts_matches_port_insensitively(tmp_path: Path) -> None:
    """Rebinding cannot change the port, so the port carries no signal - and
    the port a browser sends depends on how the app was published, which the
    operator should not have to enumerate."""
    with make_client(tmp_path, allowed_hosts="nas.example, 10.0.0.5:9999") as client:
        assert client.get("/healthz", headers={"host": "nas.example"}).status_code == 200
        assert client.get("/healthz", headers={"host": "nas.example:8420"}).status_code == 200
        assert client.get("/healthz", headers={"host": "NAS.example"}).status_code == 200
        # Port-insensitive in the setting too: a configured port is ignored
        # rather than silently never matching.
        assert client.get("/healthz", headers={"host": "10.0.0.5:8420"}).status_code == 200


def test_host_rejection_still_carries_security_headers(tmp_path: Path) -> None:
    """The Host guard registers inside the security-headers middleware, so
    even its refusal keeps the "on every response" promise those headers make.
    A regression here means the registration order flipped."""
    with make_client(tmp_path, allowed_hosts="nas.example") as client:
        response = client.get("/healthz", headers={"host": "evil.example"})
        assert response.status_code == 400
        assert "Content-Security-Policy" in response.headers


# ------------------------------------------------------------- login gets 429


def test_login_limiter_refusal_is_429_not_401(tmp_path: Path) -> None:
    """Inside the window a bad password is 401; past it, 429 - same split as
    change-password, which shares the limiter. 401 for both told a locked-out
    legitimate user their password was wrong when it was not."""
    limit = 3  # small, so the test does not hammer Argon2 five times
    with make_client(
        tmp_path, login_rate_limit=limit, login_rate_window_seconds=3600
    ) as client:
        client.post("/api/auth/bootstrap",
                    json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        for _ in range(limit):
            response = client.post("/api/auth/login",
                                   json={"username": "admin", "password": "wrong"},
                                   headers=CSRF)
            assert response.status_code == 401
        # The window is full: even the correct password is refused, and the
        # refusal names the real reason.
        blocked = client.post("/api/auth/login",
                              json={"username": "admin", "password": PASSWORD}, headers=CSRF)
        assert blocked.status_code == 429


# --------------------------------------------------------- forwarded proxy IPs


def test_forwarded_allow_ips_defaults_to_uvicorns_own(tmp_path: Path) -> None:
    """127.0.0.1 is uvicorn's default, so an unset deployment is bit-for-bit
    the pre-setting behaviour."""
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.forwarded_allow_ips == "127.0.0.1"


def test_forwarded_allow_ips_reads_the_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KTN_FORWARDED_ALLOW_IPS", "10.0.0.2")
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.forwarded_allow_ips == "10.0.0.2"


def test_entrypoint_passes_forwarded_allow_ips_to_uvicorn() -> None:
    """The setting is consumed by the entrypoint, not the app, so pin the
    wiring: every uvicorn invocation must carry the flag, sourced from the
    KTN_ variable with uvicorn's own default. Without this the setting would
    exist, document itself, and do nothing."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'FORWARDED_ALLOW_IPS="${KTN_FORWARDED_ALLOW_IPS:-127.0.0.1}"' in text
    assert text.count('--forwarded-allow-ips "$FORWARDED_ALLOW_IPS"') == 2
