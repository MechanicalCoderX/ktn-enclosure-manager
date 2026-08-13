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

from ktnmgr.enclosure.locate import LocateError, validate_request  # noqa: E402
from ktnmgr.enclosure.sysfs import (  # noqa: E402
    EnclosureNotFoundError,
    SlotNotFoundError,
    SysfsEnclosureBackend,
)

log = logging.getLogger("ktn-ident-helper")

MAX_REQUEST_BYTES = 4096


class IdentHandler(socketserver.StreamRequestHandler):
    backend: SysfsEnclosureBackend
    allowlist: set[str]

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
            state = self.backend.set_locate(ref, slot, op == "identify_on")
        except SlotNotFoundError as exc:
            raise LocateError("slot not present on this enclosure") from exc
        except OSError as exc:
            raise LocateError(f"locate write failed: {exc}") from exc

        log.info("%s enclosure=%s slot=%s -> locate=%s", op, enclosure_id, slot, state)
        return {"ok": True, "locate": state}


class IdentServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Privileged IDENT helper")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--socket-group", default=None,
                        help="group name or gid allowed to use the socket")
    parser.add_argument("--sysfs-root", default=Path("/sys"), type=Path)
    parser.add_argument("--allow", default="",
                        help="comma-separated enclosure logical ids; empty means any")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    IdentHandler.backend = SysfsEnclosureBackend(sysfs_root=args.sysfs_root)
    IdentHandler.allowlist = {e.strip().lower() for e in args.allow.split(",") if e.strip()}

    args.socket.parent.mkdir(parents=True, exist_ok=True)
    if args.socket.exists():
        args.socket.unlink()

    server = IdentServer(str(args.socket), IdentHandler)

    # Socket is group-accessible only: no world access, no ambient reachability.
    if args.socket_group is not None:
        gid = int(args.socket_group) if str(args.socket_group).isdigit() else grp.getgrnam(
            str(args.socket_group)
        ).gr_gid
        os.chown(args.socket, 0, gid)
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
