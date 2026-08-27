"""Application entrypoint and dependency wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ktnmgr import __version__
from ktnmgr.api.routes import router
from ktnmgr.config import Settings, get_settings
from ktnmgr.enclosure.disks import DiskInfoReader
from ktnmgr.enclosure.helper_client import HelperUnavailableError
from ktnmgr.enclosure.helper_client import send as helper_send
from ktnmgr.enclosure.locate import build_locate_writer
from ktnmgr.enclosure.ses import HelperSesRunner, SesRunner
from ktnmgr.enclosure.sysfs import SysfsEnclosureBackend
from ktnmgr.services.audit import AuditLog
from ktnmgr.services.auth import AuthService
from ktnmgr.services.ident import IdentManager
from ktnmgr.services.notify import HealthNotifier
from ktnmgr.services.state import StateService
from ktnmgr.truenas.client import TrueNASClient

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def _canonical_host(value: str) -> str:
    """Lowercased host with any port stripped, for KTN_ALLOWED_HOSTS matching.

    Matching is port-insensitive on purpose: DNS rebinding can change what a
    hostname resolves to but never the port the browser sends, so the port
    adds nothing to the check - and the port a legitimate browser sends
    depends on how the deployment published the app, which the operator
    should not have to enumerate. A bracketed IPv6 literal ([::1]:8420) loses
    brackets and port; an unbracketed one (::1) has multiple colons and no
    port syntax, so it is left whole rather than mangled by a rsplit.
    """
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    lock_path = settings.enclosure_lock_path
    backend = SysfsEnclosureBackend(
        sysfs_root=settings.sysfs_root, dev_root=settings.dev_root, lock_path=lock_path
    )
    disks = DiskInfoReader(sysfs_root=settings.sysfs_root)
    # When a helper socket is configured the web process reads SES pages
    # through it, so it needs no access to /dev/sg* at all.
    ses = (
        HelperSesRunner(settings.ident_helper_socket)
        if settings.ident_helper_socket
        else SesRunner(binary=settings.sg_ses_binary, lock_path=lock_path)
    )
    audit = AuditLog(settings.audit_path)
    writer = build_locate_writer(
        backend,
        settings.ident_helper_socket,
        ses=SesRunner(binary=settings.sg_ses_binary, lock_path=lock_path),
        method=settings.ident_method,
    )
    ident = IdentManager(writer=writer, audit=audit, state_path=settings.ident_state_path)

    truenas: TrueNASClient | None = None
    if settings.truenas_url and settings.truenas_api_key.get_secret_value():
        truenas = TrueNASClient(
            url=settings.truenas_url,
            api_key=settings.truenas_api_key,
            verify_tls=settings.truenas_verify_tls,
            ca_bundle=settings.truenas_ca_bundle,
            rest_fallback=settings.truenas_rest_fallback,
        )
    else:
        log.warning("TrueNAS not configured; pool, vdev and SMART data will be unavailable")

    notifier = HealthNotifier(
        url=settings.alert_webhook_url,
        style=settings.alert_style,
        state_path=settings.notify_state_path,
        notify_recovery=settings.alert_on_recovery,
    )
    if notifier.enabled:
        log.info("health notifications enabled (%s)", settings.alert_style)

    service = StateService(
        settings=settings, backend=backend, disks=disks, ses=ses, ident=ident,
        truenas=truenas, notifier=notifier,
    )
    auth = AuthService(
        users_path=settings.users_path,
        secret_path=settings.secret_path,
        session_secret=settings.session_secret.get_secret_value() or None,
        max_age_seconds=settings.session_max_age_seconds,
        rate_limit=settings.login_rate_limit,
        rate_window=settings.login_rate_window_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        await service.start()
        found = service.enclosures.value
        log.info(
            "startup complete: %d enclosure(s): %s",
            len(found),
            ", ".join(f"{e.vendor} {e.product} {e.logical_id}" for e in found) or "none",
        )
        if auth.needs_bootstrap:
            log.warning("no administrator account yet - open the UI to create one")
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="KTN Enclosure Manager",
        version=__version__,
        description="Local enclosure management for SES disk shelves on TrueNAS SCALE.",
        lifespan=lifespan,
    )

    allowed_hosts = {
        _canonical_host(entry)
        for entry in settings.allowed_hosts.split(",")
        if entry.strip()
    }
    if allowed_hosts:
        # Registered before security_headers, which therefore wraps it (later
        # registration is outermost), so even this rejection carries the
        # defence-in-depth headers that middleware promises on every response.
        @app.middleware("http")
        async def enforce_host(request: Any, call_next: Any) -> Any:
            """Refuse requests whose Host is not one this app answers to.

            This is the DNS-rebinding defence (see Settings.allowed_hosts): a
            hostile page can repoint its own hostname at this app's address,
            and the browser will then treat the app as same-origin with the
            attacker - the session cookie does not travel (it is keyed to the
            real hostname), so with authentication on the API answers 401
            anyway, but the opt-in anonymous modes answer with data. The Host
            header is the one thing rebinding cannot forge: it still names the
            attacker's domain. An absent Host header is refused too - failing
            open there would exempt exactly the hand-crafted requests.
            """
            host = request.headers.get("host")
            if host is None or _canonical_host(host) not in allowed_hosts:
                return JSONResponse(
                    {"detail": "request Host header does not match KTN_ALLOWED_HOSTS"},
                    status_code=400,
                )
            return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Any, call_next: Any) -> Any:
        """Defence-in-depth headers on every response.

        The one that matters here is frame-ancestors: without it another page
        could frame this UI and trick a click onto Identify. The CSP is strict
        because the bundle is entirely self-hosted - no CDN, no inline script,
        no external fonts - so nothing legitimate needs relaxing.
        """
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "   # Vite emits a style attribute or two
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), usb=()"
        )
        # Only meaningful over TLS, and harmless otherwise; omitted deliberately
        # because this app is normally served over plain HTTP on a LAN and a
        # stray HSTS header would pin a hostname the user cannot serve over TLS.
        return response

    app.state.settings = settings
    app.state.service = service
    app.state.auth = auth
    app.state.audit = audit
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness for the container healthcheck.

        When a privileged helper is configured it is part of the deployment's
        health, not an optional extra: without it the IDENT write and all SES
        telemetry are gone. It runs as a background child of the entrypoint
        with nothing supervising it, so a helper that died would otherwise
        leave the container Healthy while the app silently lost half its
        function. Probing it here is what makes the healthcheck honest.
        """
        body: dict[str, Any] = {"ok": True, "enclosures": len(service.enclosures.value)}
        socket_path = settings.ident_helper_socket
        if socket_path:
            try:
                response = helper_send(socket_path, {"op": "ses_version"}, timeout=4.0)
                body["helper"] = "ok" if response.get("ok") else "error"
            except HelperUnavailableError as exc:
                body.update(ok=False, helper="unreachable", helper_error=str(exc))
                return JSONResponse(body, status_code=503)
        return JSONResponse(body)

    if FRONTEND_DIR.is_dir():
        app.mount(
            "/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets"
        )

        # Resolved once; every request is checked against this real path.
        frontend_root = FRONTEND_DIR.resolve()
        index_html = frontend_root / "index.html"

        @app.get("/{path:path}")
        def spa(path: str) -> FileResponse:
            """Serve the SPA, refusing to serve anything outside the bundle.

            The containment check below is essential and was missing.
            ``Path / path`` does NOT confine the result: an absolute ``path``
            replaces the base entirely (``Path("/a/b") / "/etc/passwd"`` is
            ``/etc/passwd``) and ``..`` segments walk out of it. This route is
            unauthenticated, so that was an arbitrary file read - including
            /data/session-secret, which signs session cookies, and
            /data/users.json.

            Resolve first, then require the result to be inside the bundle.
            Resolving also collapses symlinks, so a link planted inside the
            bundle cannot point outside it either.
            """
            if not path:
                return FileResponse(index_html)
            try:
                candidate = (frontend_root / path).resolve()
            except (OSError, RuntimeError, ValueError):
                # RuntimeError: symlink loop. ValueError: embedded NUL byte.
                return FileResponse(index_html)
            if candidate.is_relative_to(frontend_root) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_html)
    else:
        log.warning("frontend bundle not found at %s; API only", FRONTEND_DIR)

    return app


app = build_app()
