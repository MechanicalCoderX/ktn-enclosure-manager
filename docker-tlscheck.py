#!/usr/bin/env python3
"""Check the TLS pair the entrypoint copied, before uvicorn is started on it.

    python /app/docker-tlscheck.py CERT KEY

Run by docker-entrypoint.sh as uid 1000 against the tmpfs copies. Exit 0
when the certificate chain parses, the key parses and the two match; exit 1
otherwise, and the entrypoint then refuses to start (it never falls back to
plain HTTP).

An expired certificate is a WARNING, not a failure: it is still served.
Refusing it would turn a browser warning into an outage, and the operator
still needs the app (by IP, over https) while sorting out the renewal.

Prints the subject CN, notAfter and days left - never key or certificate
text. Standard library only, like docker-healthcheck.py.
"""

from __future__ import annotations

import ssl
import sys
import time


def _no_passphrase() -> bytes:
    # An encrypted key cannot be served unattended. Answering the passphrase
    # callback (with nothing) makes it fail here instead of OpenSSL falling
    # back to prompting on a terminal that does not exist.
    return b""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: docker-tlscheck.py CERT KEY", file=sys.stderr)
        return 2
    cert, key = argv[1], argv[2]

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert, key, password=_no_passphrase)
    except ssl.SSLError as exc:
        # The reason code (e.g. KEY_VALUES_MISMATCH) only - the message
        # carries no file content, but nothing here needs more than the code.
        print(f"TLS check failed: {exc.reason or type(exc).__name__}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"TLS check failed: {exc.strerror or type(exc).__name__}", file=sys.stderr)
        return 1

    try:
        info = ssl._ssl._test_decode_cert(cert)  # type: ignore[attr-defined]
        not_after = info["notAfter"]
        expires = ssl.cert_time_to_seconds(not_after)
        cn = next(
            (value for rdn in info.get("subject", ()) for name, value in rdn
             if name == "commonName"),
            "?",
        )
    except Exception as exc:  # noqa: BLE001 - private helper; the pair is valid regardless
        print(f"TLS certificate OK (dates unavailable: {type(exc).__name__})")
        return 0

    days = int((expires - time.time()) // 86400)
    if days < 0:
        print(
            f"WARNING: TLS certificate CN={cn} EXPIRED (notAfter {not_after}); "
            "serving it anyway - renew it, and reach the app by IP over https meanwhile"
        )
    else:
        print(f"TLS certificate OK: CN={cn} notAfter={not_after} ({days} days left)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
