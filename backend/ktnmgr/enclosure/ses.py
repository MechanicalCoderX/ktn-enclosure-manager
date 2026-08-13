"""Safe, read-only sg_ses invocation.

Chassis management is strictly read-only in version 1 (spec §15). This module
can only run an allow-listed set of diagnostic page reads; there is no code
path that reaches ``--set``, ``--clear``, ``--control`` or any other mutating
sg_ses option, so a compromised or buggy caller cannot reach one.

Command construction never uses a shell (§30). The device path is not taken
from the caller as free text either: it is resolved from a discovered
enclosure, so browser input can never become an argv element.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: S404 - argv-only, shell=False, allow-listed arguments
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# The only sg_ses invocations this application is capable of making.
# Keys are semantic names used internally; values are literal argv fragments.
READ_ONLY_PAGES: dict[str, tuple[str, ...]] = {
    "configuration": ("-p", "cf"),
    "enclosure_status": ("-p", "es"),
    "additional_element_status": ("-p", "aes"),
    "join": ("--join",),
    "join_filtered": ("--join", "--filter"),
}

DEFAULT_TIMEOUT = 20.0


class SesError(RuntimeError):
    """sg_ses could not be run, timed out, or returned a failure."""


@dataclass(frozen=True)
class SesResult:
    page: str
    stdout: str
    returncode: int


class SesRunner:
    """Runs allow-listed read-only sg_ses pages against a resolved device."""

    def __init__(self, binary: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._binary = binary or shutil.which("sg_ses") or "/usr/bin/sg_ses"
        self.timeout = timeout

    @property
    def binary(self) -> str:
        return self._binary

    def available(self) -> bool:
        return Path(self._binary).exists()

    def version(self) -> str | None:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False
                [self._binary, "--version"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return (proc.stdout or proc.stderr).strip() or None

    def read_page(self, device: str, page: str) -> SesResult:
        """Run one allow-listed read-only page against ``device``.

        ``device`` must be a path produced by enclosure discovery. ``page`` must
        be a key of READ_ONLY_PAGES; anything else raises before any process is
        created.
        """
        if page not in READ_ONLY_PAGES:
            raise SesError(f"page {page!r} is not an allow-listed read-only page")

        device_path = Path(device)
        if not device_path.is_absolute() or device_path.parent.name in ("", "."):
            raise SesError(f"refusing non-absolute device path {device!r}")

        argv = [self._binary, *READ_ONLY_PAGES[page], str(device_path)]
        log.debug("running %s", argv)
        try:
            proc = subprocess.run(  # noqa: S603 - argv built from allow-list only
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SesError(f"sg_ses timed out after {self.timeout}s reading {page}") from exc
        except OSError as exc:
            raise SesError(f"could not execute {self._binary}: {exc}") from exc

        if proc.returncode != 0 and not proc.stdout.strip():
            raise SesError(
                f"sg_ses {page} failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
            )
        return SesResult(page=page, stdout=proc.stdout, returncode=proc.returncode)
