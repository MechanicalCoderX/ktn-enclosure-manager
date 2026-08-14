#!/usr/bin/env python3
"""Privileged IDENT helper (spec §31).

The only privileged component. It accepts exactly three semantic operations
over a unix socket and can express nothing else:

    identify_on(enclosure_id, slot)
    identify_off(enclosure_id, slot)
    identify_read(enclosure_id, slot)

It never receives a path, a command, or an sg_ses argument. The enclosure is
resolved from its logical identifier by scanning sysfs here, inside the
privileged process, so a caller cannot point it at an arbitrary file even if
the web process is fully compromised.

Run as root (or with CAP_DAC_OVERRIDE); the web process runs unprivileged and
only needs group access to the socket.

    ktn_ident_helper.py --socket /run/ktn/ident.sock --socket-group 1000
"""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import socketserver
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from ktnmgr.enclosure.access import default_lock_path
from ktnmgr.enclosure.locate import (
    LocateError,
    build_local_locate_writer,
    validate_request,
)
from ktnmgr.enclosure.ses import READ_ONLY_PAGES, SesError, SesRunner
from ktnmgr.enclosure.sysfs import (
    EnclosureNotFoundError,
    SlotNotFoundError,
    SysfsEnclosureBackend,
)

log = logging.getLogger("ktn-ident-helper")

MAX_REQUEST_BYTES = 4096


class IdentHandler(socketserver.StreamRequestHandler):
    backend: SysfsEnclosureBackend
    allowlist: set[str]
    ses: SesRunner
    writer: object

    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES)
        try:
            response = self._dispatch(raw)
        except Exception as exc:  # noqa: BLE001 - a helper must never die on input
            log.warning("rejected request: %s", exc)
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))

    def _dispatch(self, raw: bytes) -> dict[str, object]:
        try:
            request = json.loads(raw.decode("utf-8").strip() or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise LocateError("malformed request") from exc
        if not isinstance(request, dict):
            raise LocateError("request must be an object")

        op = request.get("op")

        # Read-only SES telemetry. The web process has no access to /dev/sg*,
        # so these reads also cross the privilege boundary. The page name is
        # checked against the same allow-list, and the device is resolved here
        # from the enclosure id - never supplied by the caller.
        if op == "ses_version":
            return {"ok": True, "version": self.ses.version()}

        if op == "ses_read":
            page = request.get("page")
            if page not in READ_ONLY_PAGES:
                raise LocateError("page is not an allow-listed read-only page")
            enclosure_id, _ = validate_request(request.get("enclosure_id"), 0)
            if self.allowlist and enclosure_id not in self.allowlist:
                raise LocateError("enclosure not in allowlist")
            try:
                ref = self.backend.resolve(enclosure_id)
            except EnclosureNotFoundError as exc:
                raise LocateError("enclosure not attached") from exc
            if not ref.sg_device:
                raise LocateError("enclosure has no sg device")
            try:
                result = self.ses.read_page(ref.sg_device, page)
            except SesError as exc:
                raise LocateError(str(exc)) from exc
            return {"ok": True, "output": result.stdout}

        if op not in ("identify_on", "identify_off", "identify_read"):
            raise LocateError("unsupported operation")

        # Re-validated here, independently of the caller. The web process is
        # not trusted to have validated anything.
        enclosure_id, slot = validate_request(request.get("enclosure_id"), request.get("slot"))

        if self.allowlist and enclosure_id not in self.allowlist:
            raise LocateError("enclosure not in allowlist")

        try:
            ref = self.backend.resolve(enclosure_id)
        except EnclosureNotFoundError as exc:
            raise LocateError("enclosure not attached") from exc

        try:
            if op == "identify_read":
                return {"ok": True, "locate": self.backend.read_locate(ref, slot)}
            state = self.writer.write(enclosure_id, slot, op == "identify_on")
        except SlotNotFoundError as exc:
            raise LocateError("slot not present on this enclosure") from exc
        except OSError as exc:
            raise LocateError(f"locate write failed: {exc}") from exc

        log.info("%s enclosure=%s slot=%s -> locate=%s", op, enclosure_id, slot, state)
        return {"ok": True, "locate": state}


class IdentServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def _bind_with_group(socket_path: Path, gid: int | None) -> IdentServer:
    """Create the listening socket so it carries ``gid``.

    A unix socket takes the creating process's effective gid, so the group is
    set by binding under it rather than by chown'ing afterwards - which would
    need CAP_CHOWN, a capability this container deliberately does not have.

    If the egid cannot be changed the socket is still created; the caller then
    reports the mismatch rather than the helper refusing to start, because a
    running app with a warning beats no app at all.
    """
    if gid is None:
        return IdentServer(str(socket_path), IdentHandler)

    previous = os.getegid()
    changed = False
    try:
        os.setegid(gid)
        changed = True
    except OSError as exc:
        log.warning("could not assume gid %s for the socket (%s)", gid, exc)

    try:
        return IdentServer(str(socket_path), IdentHandler)
    finally:
        if changed:
            try:
                os.setegid(previous)
            except OSError:  # pragma: no cover - would mean losing CAP_SETGID
                log.error("could not restore gid %s after binding", previous)


def main() -> int:
    parser = argparse.ArgumentParser(description="Privileged IDENT helper")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--socket-group", default=None,
                        help="group name or gid allowed to use the socket")
    parser.add_argument("--sysfs-root", default=Path("/sys"), type=Path)
    parser.add_argument("--allow", default="",
                        help="comma-separated enclosure logical ids; empty means any")
    parser.add_argument("--ident-method", default="auto", choices=("auto", "ses", "sysfs"),
                        help="how to drive the LED; 'ses' avoids needing a writable /sys")
    # Must be the same file the web process uses, or the two processes will
    # not exclude each other and the shelf will abort colliding commands.
    parser.add_argument("--enclosure-lock", default=None, type=Path,
                        help="cross-process lock file serialising enclosure access")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    lock_path = args.enclosure_lock or default_lock_path()
    IdentHandler.backend = SysfsEnclosureBackend(
        sysfs_root=args.sysfs_root, lock_path=lock_path
    )
    IdentHandler.allowlist = {e.strip().lower() for e in args.allow.split(",") if e.strip()}
    IdentHandler.ses = SesRunner(lock_path=lock_path)
    IdentHandler.writer = build_local_locate_writer(
        IdentHandler.backend, IdentHandler.ses, args.ident_method
    )

    args.socket.parent.mkdir(parents=True, exist_ok=True)
    if args.socket.exists():
        args.socket.unlink()

    gid: int | None = None
    if args.socket_group is not None:
        gid = int(args.socket_group) if str(args.socket_group).isdigit() else grp.getgrnam(
            str(args.socket_group)
        ).gr_gid

    # Bind with the socket's group as our effective gid, so the socket is
    # created carrying it. A unix socket inherits the creating process's egid
    # unless the directory is setgid.
    #
    # Doing it here rather than relying on a setgid directory is what makes
    # this work everywhere: the TrueNAS catalog library validates tmpfs modes
    # against ^0[0-7]{3}$ and cannot express setgid at all, and an earlier
    # attempt to add the bit with chmod silently cleared it instead (no
    # CAP_FSETID). Changing egid needs CAP_SETGID, which this container does
    # hold - it is one of the two capabilities kept, for setpriv.
    server = _bind_with_group(args.socket, gid)

    # Belt and braces for a deployment that does grant CAP_CHOWN, and a clear
    # warning if the group still is not right rather than a silent failure.
    if gid is not None:
        try:
            if os.stat(args.socket).st_gid != gid:
                os.chown(args.socket, 0, gid)
        except PermissionError:
            actual = os.stat(args.socket).st_gid
            if actual != gid:
                log.warning(
                    "cannot set socket group to %s (currently %s); the web process "
                    "may be unable to connect", gid, actual,
                )
            else:
                log.debug("socket already carries group %s; chown not required", gid)
    os.chmod(args.socket, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)

    found = IdentHandler.backend.discover()
    log.info(
        "listening on %s; %d enclosure(s) visible: %s",
        args.socket, len(found), ", ".join(e.logical_id for e in found) or "none",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
        if args.socket.exists():
            args.socket.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
