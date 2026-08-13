"""Minimal newline-delimited JSON client for the privileged helper.

Shared by the IDENT writer and the SES reader so both cross the privilege
boundary through exactly one code path.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class HelperUnavailableError(RuntimeError):
    """The helper socket could not be reached or spoke nonsense."""


def send(
    socket_path: Path, payload: dict[str, Any], timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Send one request and read one newline-terminated JSON response."""
    encoded = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(encoded)

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise HelperUnavailableError("helper response too large")
                if chunk.endswith(b"\n"):
                    break
    except OSError as exc:
        raise HelperUnavailableError(f"helper unreachable: {exc}") from exc

    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    try:
        response = json.loads(raw or "{}")
    except ValueError as exc:
        raise HelperUnavailableError("helper returned malformed JSON") from exc
    if not isinstance(response, dict):
        raise HelperUnavailableError("helper returned a non-object response")
    return response
