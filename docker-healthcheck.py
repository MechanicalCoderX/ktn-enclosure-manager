#!/usr/bin/env python3
"""The image's HEALTHCHECK: GET /healthz over loopback, plain or TLS.

Exit 0 on a 200, 1 on anything else. It replaces an inline command that
hard-coded http://127.0.0.1:8420 - which ignored KTN_PORT, and would report a
TLS deployment unhealthy forever because plain HTTP to a TLS port gets an
empty reply. Docker runs it with the container's environment, so it follows
the same KTN_PORT and KTN_TLS_ENABLED the entrypoint used.

The TLS probe does not verify the certificate. It is a loopback liveness
check, not an authentication: the certificate names the public host, never
127.0.0.1, so a verifying probe could only fail. Standard library only.
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib.request

# Same spelling table as KTN_TLS_ENABLED everywhere else (backend/ktnmgr/
# config.py, scripts/tls-bool.sh). An unrecognised value never gets this far:
# the entrypoint refuses to start on one.
_TRUE = {"1", "true", "yes", "on"}


def main() -> int:
    try:
        port = int(os.environ.get("KTN_PORT", "8420"))
        tls = os.environ.get("KTN_TLS_ENABLED", "").lower() in _TRUE
        if tls:
            url = f"https://127.0.0.1:{port}/healthz"
            context = ssl._create_unverified_context()  # noqa: S323 - loopback liveness probe
        else:
            url = f"http://127.0.0.1:{port}/healthz"
            context = None
        with urllib.request.urlopen(url, timeout=4, context=context) as response:  # noqa: S310
            return 0 if response.status == 200 else 1
    except Exception:  # noqa: BLE001 - any failure is "unhealthy"
        return 1


if __name__ == "__main__":
    sys.exit(main())
