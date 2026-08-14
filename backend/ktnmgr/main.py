"""Application entrypoint and dependency wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ktnmgr import __version__
from ktnmgr.api.routes import router
from ktnmgr.config import Settings, get_settings
from ktnmgr.enclosure.disks import DiskInfoReader
from ktnmgr.enclosure.locate import build_locate_writer
from ktnmgr.enclosure.ses import HelperSesRunner, SesRunner
from ktnmgr.enclosure.sysfs import SysfsEnclosureBackend
from ktnmgr.services.audit import AuditLog
from ktnmgr.services.auth import AuthService
from ktnmgr.services.ident import IdentManager
from ktnmgr.services.state import StateService
from ktnmgr.truenas.client import TrueNASClient

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    backend = SysfsEnclosureBackend(sysfs_root=settings.sysfs_root, dev_root=settings.dev_root)
    disks = DiskInfoReader(sysfs_root=settings.sysfs_root)
    # When a helper socket is configured the web process reads SES pages
    # through it, so it needs no access to /dev/sg* at all.
    ses = (
        HelperSesRunner(settings.ident_helper_socket)
        if settings.ident_helper_socket
        else SesRunner(binary=settings.sg_ses_binary)
    )
    audit = AuditLog(settings.audit_path)
    writer = build_locate_writer(
        backend,
        settings.ident_helper_socket,
        ses=SesRunner(binary=settings.sg_ses_binary),
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
        )
    else:
        log.warning("TrueNAS not configured; pool, vdev and SMART data will be unavailable")

    service = StateService(
        settings=settings, backend=backend, disks=disks, ses=ses, ident=ident, truenas=truenas
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
    app.state.settings = settings
    app.state.service = service
    app.state.auth = auth
    app.state.audit = audit
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "enclosures": len(service.enclosures.value)})

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
