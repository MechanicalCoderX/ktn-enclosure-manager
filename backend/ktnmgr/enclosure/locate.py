"""IDENT write paths.

Two implementations of the same narrow interface (spec §31):

``DirectLocateWriter``
    Writes ``<enclosure>/<slot>/locate`` itself. Used when the process already
    has permission - development, or a deployment that accepts a writable
    sysfs bind mount.

``HelperLocateWriter``
    Sends a *semantic* request over a unix socket to a tiny privileged helper.
    The web process then needs no elevated privilege at all and cannot express
    a filesystem path, only ``identify_on(enclosure_id, slot)``.

Both accept only an enclosure logical id and an integer slot. Neither has a
parameter through which a path, a command, or an sg_ses argument could travel.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from ktnmgr.enclosure.helper_client import HelperUnavailableError, send
from ktnmgr.enclosure.ses import SesError, SesRunner
from ktnmgr.enclosure.ses_parser import array_slot_type_index, parse_configuration
from ktnmgr.enclosure.sysfs import (
    DEFAULT_SETTLE_POLL,
    DEFAULT_SETTLE_TIMEOUT,
    SysfsEnclosureBackend,
)

log = logging.getLogger(__name__)

VALID_OPS = ("identify_on", "identify_off", "identify_read")

#: An enclosure logical id as exposed by /sys/class/enclosure/<x>/id.
#: Anchored and hex-only so no traversal or shell metacharacter can survive.
import re  # noqa: E402

ENCLOSURE_ID_RE = re.compile(r"^0x[0-9a-f]{4,32}$")
MAX_SLOT = 1023


class LocateError(RuntimeError):
    """An IDENT operation could not be performed."""


def validate_request(enclosure_id: str, slot: int) -> tuple[str, int]:
    """Semantic validation applied identically on both sides of the socket.

    Rejects everything in the spec's hostile-input list (§43) by construction:
    the id must match a hex pattern and the slot must be a bounded integer, so
    ``7;rm -rf /``, ``../../etc/passwd``, ``$(id)``, ``7 --set=device_off`` and
    ``/dev/sg0`` cannot be represented at all.
    """
    if not isinstance(enclosure_id, str):
        raise LocateError("enclosure id must be a string")
    normalised = enclosure_id.strip().lower()
    if not ENCLOSURE_ID_RE.match(normalised):
        raise LocateError("enclosure id is not a valid logical identifier")

    if isinstance(slot, bool) or not isinstance(slot, int):
        raise LocateError("slot must be an integer")
    if slot < 0 or slot > MAX_SLOT:
        raise LocateError(f"slot {slot} out of range")
    return normalised, slot


@runtime_checkable
class LocateWriter(Protocol):
    def read(self, enclosure_id: str, slot: int) -> bool: ...
    def write(self, enclosure_id: str, slot: int, on: bool) -> bool: ...


class DirectLocateWriter:
    """Writes sysfs in-process. Requires permission on the locate attribute."""

    def __init__(self, backend: SysfsEnclosureBackend) -> None:
        self.backend = backend

    def read(self, enclosure_id: str, slot: int) -> bool:
        enclosure_id, slot = validate_request(enclosure_id, slot)
        ref = self.backend.resolve(enclosure_id)
        return self.backend.read_locate(ref, slot)

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        enclosure_id, slot = validate_request(enclosure_id, slot)
        ref = self.backend.resolve(enclosure_id)
        try:
            return self.backend.set_locate(ref, slot, on)
        except OSError as exc:
            raise LocateError(f"locate write failed: {exc}") from exc


class SesLocateWriter:
    """Drives IDENT with a SCSI command instead of a sysfs write.

    This is the preferred write path, and the reason is deployment rather than
    elegance. Docker's default AppArmor profile denies every write under /sys
    regardless of uid, capabilities or mount flags, so the sysfs path forces
    `apparmor=unconfined` on the container. SG_IO is not restricted by that
    profile, so going through the enclosure device instead means the container
    needs no AppArmor relaxation, no CAP_DAC_OVERRIDE, and no writable /sys.

    Verification still reads the sysfs `locate` attribute, which is a read and
    therefore always permitted. The same settle-polling applies: the attribute
    is refreshed only once the enclosure processor answers.
    """

    def __init__(self, backend: SysfsEnclosureBackend, ses: SesRunner) -> None:
        self.backend = backend
        self.ses = ses
        self._type_index: dict[str, int] = {}

    def _array_type_index(self, ref: object) -> int:
        """Which SES type descriptor holds the drive bays.

        It is 0 on the KTN-STL3, but that is not guaranteed elsewhere, so it is
        read from the configuration page and cached per enclosure.
        """
        logical_id = ref.logical_id  # type: ignore[attr-defined]
        if logical_id not in self._type_index:
            device = getattr(ref, "sg_device", None)
            if not device:
                raise LocateError("enclosure has no sg device for SES IDENT")
            try:
                page = self.ses.read_page(device, "configuration")
            except SesError as exc:
                raise LocateError(f"could not read SES configuration: {exc}") from exc
            index = array_slot_type_index(parse_configuration(page.stdout)[1])
            if index is None:
                raise LocateError("enclosure reports no array device slot elements")
            self._type_index[logical_id] = index
        return self._type_index[logical_id]

    def read(self, enclosure_id: str, slot: int) -> bool:
        enclosure_id, slot = validate_request(enclosure_id, slot)
        return self.backend.read_locate(self.backend.resolve(enclosure_id), slot)

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        enclosure_id, slot = validate_request(enclosure_id, slot)
        ref = self.backend.resolve(enclosure_id)
        type_index = self._array_type_index(ref)

        try:
            self.ses.set_ident(ref.sg_device, type_index, slot, on)
        except SesError as exc:
            raise LocateError(f"IDENT command failed: {exc}") from exc

        target = self.backend.slot_dir(ref, slot) / "locate"
        deadline = time.monotonic() + DEFAULT_SETTLE_TIMEOUT
        observed = self.backend._read_locate_at(target)
        while observed is not on and time.monotonic() < deadline:
            time.sleep(DEFAULT_SETTLE_POLL)
            observed = self.backend._read_locate_at(target)
        return observed


class HelperLocateWriter:
    """Talks to the privileged helper over a unix socket."""

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def _request(self, op: str, enclosure_id: str, slot: int) -> bool:
        if op not in VALID_OPS:
            raise LocateError(f"unsupported operation {op!r}")
        enclosure_id, slot = validate_request(enclosure_id, slot)

        try:
            response = send(
                self.socket_path,
                {"op": op, "enclosure_id": enclosure_id, "slot": slot},
                timeout=self.timeout,
            )
        except HelperUnavailableError as exc:
            raise LocateError(str(exc)) from exc

        if not response.get("ok"):
            raise LocateError(str(response.get("error") or "IDENT helper refused the request"))
        return bool(response.get("locate"))

    def read(self, enclosure_id: str, slot: int) -> bool:
        return self._request("identify_read", enclosure_id, slot)

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        return self._request("identify_on" if on else "identify_off", enclosure_id, slot)


def build_local_locate_writer(
    backend: SysfsEnclosureBackend, ses: SesRunner, method: str = "auto"
) -> LocateWriter:
    """Choose how IDENT is actually written, in the process that will do it.

    ``auto`` prefers the SES command path because it works under the default
    container confinement; sysfs is used only if sg_ses is unavailable.
    """
    if method not in ("auto", "ses", "sysfs"):
        raise LocateError(f"unknown ident method {method!r}")

    if method == "sysfs":
        log.info("IDENT via sysfs locate write (requires writable /sys)")
        return DirectLocateWriter(backend)

    if ses.available():
        log.info("IDENT via SES command (no AppArmor relaxation or writable /sys needed)")
        return SesLocateWriter(backend, ses)

    if method == "ses":
        raise LocateError("ident_method=ses but sg_ses is not available")

    log.warning("sg_ses unavailable; falling back to sysfs IDENT, which needs a writable /sys")
    return DirectLocateWriter(backend)


def build_locate_writer(
    backend: SysfsEnclosureBackend,
    helper_socket: Path | None,
    ses: SesRunner | None = None,
    method: str = "auto",
) -> LocateWriter:
    if helper_socket:
        log.info("IDENT writes delegated to privileged helper at %s", helper_socket)
        return HelperLocateWriter(helper_socket)
    return build_local_locate_writer(backend, ses or SesRunner(), method)
