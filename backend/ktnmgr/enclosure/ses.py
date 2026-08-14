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
from typing import Any

from ktnmgr.enclosure.access import enclosure_access

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

#: The ONLY mutating SES operations this application can perform. Both address
#: a single array-device-slot element's identify bit. There is no code path to
#: any other --set/--clear target (device_off, fault, PHY reset, ...), and the
#: element is addressed by two integers that are range-checked before use.
IDENT_ARGS: dict[bool, str] = {True: "--set=ident", False: "--clear=ident"}


class SesError(RuntimeError):
    """sg_ses could not be run, timed out, or returned a failure."""


@dataclass(frozen=True)
class SesResult:
    page: str
    stdout: str
    returncode: int


class SesRunner:
    """Runs allow-listed read-only sg_ses pages against a resolved device."""

    def __init__(
        self,
        binary: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        lock_path: Path | str | None = None,
    ) -> None:
        self._binary = binary or shutil.which("sg_ses") or "/usr/bin/sg_ses"
        self.timeout = timeout
        # Serialised against the sysfs reader in the web process; see
        # enclosure/access.py. --version needs no lock: it touches no device.
        self.lock_path = lock_path

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
            with enclosure_access(self.lock_path):
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

    def read_for(self, ref: Any, page: str) -> SesResult:
        """Read a page for a discovered enclosure. Shared interface with
        HelperSesRunner so callers never have to know which one is in use."""
        if not getattr(ref, "sg_device", None):
            raise SesError("enclosure has no sg device")
        return self.read_page(ref.sg_device, page)

    def set_ident(self, device: str, type_index: int, element_index: int, on: bool) -> None:
        """Set or clear one array device slot's identify bit.

        This is the application's only mutating SES call. It reaches the LED
        through a SCSI command on the enclosure device rather than through a
        sysfs write, which matters for deployment: Docker's default AppArmor
        profile denies every write under /sys, but does not touch SG_IO. Using
        this path means the container needs no AppArmor relaxation, no
        CAP_DAC_OVERRIDE and no writable /sys.

        ``--index=`` is used rather than ``--dev-slot-num=``, which behaved
        inconsistently on sg_ses 2.48.
        """
        for name, value in (("type_index", type_index), ("element_index", element_index)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1023:
                raise SesError(f"{name} must be an integer in 0..1023")

        device_path = Path(device)
        if not device_path.is_absolute():
            raise SesError(f"refusing non-absolute device path {device!r}")

        argv = [
            self._binary,
            f"--index={type_index},{element_index}",
            IDENT_ARGS[bool(on)],
            str(device_path),
        ]
        try:
            with enclosure_access(self.lock_path):
                proc = subprocess.run(  # noqa: S603 - argv from validated ints + fixed literals
                    argv, shell=False, capture_output=True, text=True,
                    timeout=self.timeout, check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise SesError(f"sg_ses ident timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise SesError(f"could not execute {self._binary}: {exc}") from exc

        if proc.returncode != 0:
            raise SesError(
                f"sg_ses ident failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
            )


class HelperSesRunner:
    """Reads allow-listed sg_ses pages through the privileged helper.

    With this in place the web process needs no access to /dev/sg* at all: the
    device node stays root-owned and only the helper opens it. The page name is
    still validated on both sides, and the helper resolves the device from the
    enclosure's logical id rather than accepting a path.
    """

    def __init__(self, socket_path: Path, timeout: float = 40.0) -> None:
        from ktnmgr.enclosure.helper_client import HelperUnavailableError, send

        self._send = send
        self._unavailable = HelperUnavailableError
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._version: str | None = None

    @property
    def binary(self) -> str:
        return f"(privileged helper at {self.socket_path})"

    def available(self) -> bool:
        return self.socket_path.exists()

    def version(self) -> str | None:
        if self._version is None:
            try:
                response = self._send(
                    self.socket_path, {"op": "ses_version"}, timeout=self.timeout
                )
                self._version = str(response.get("version") or "") or None
            except self._unavailable:
                return None
        return self._version

    def read_for(self, ref: Any, page: str) -> SesResult:
        return self.read_page_for(ref.logical_id, page)

    def read_page_for(self, enclosure_id: str, page: str) -> SesResult:
        if page not in READ_ONLY_PAGES:
            raise SesError(f"page {page!r} is not an allow-listed read-only page")
        try:
            response = self._send(
                self.socket_path,
                {"op": "ses_read", "enclosure_id": enclosure_id, "page": page},
                timeout=self.timeout,
            )
        except self._unavailable as exc:
            raise SesError(str(exc)) from exc
        if not response.get("ok"):
            raise SesError(str(response.get("error") or "helper refused the SES read"))
        return SesResult(page=page, stdout=str(response.get("output") or ""), returncode=0)
