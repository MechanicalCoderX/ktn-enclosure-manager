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
import re
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from ktnmgr.enclosure.access import enclosure_access
from ktnmgr.enclosure.helper_client import HelperUnavailableError, send
from ktnmgr.enclosure.ses import SesError, SesRunner
from ktnmgr.enclosure.ses_parser import (
    BAY_ELEMENT_TYPES,
    array_slot_type_index,
    parse_additional_element_status,
    parse_configuration,
)
from ktnmgr.enclosure.sysfs import (
    DEFAULT_SETTLE_POLL,
    DEFAULT_SETTLE_TIMEOUT,
    SysfsEnclosureBackend,
)

log = logging.getLogger(__name__)

VALID_OPS = ("identify_on", "identify_off", "identify_read")

#: An enclosure logical id as exposed by /sys/class/enclosure/<x>/id.
#: Anchored and hex-only so no traversal or shell metacharacter can survive.
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

    The caller's slot number and sg_ses's element index are NOT the same
    coordinate system. The slot arriving here originates from the sysfs `slot`
    attribute, which the kernel ses driver fills from the additional element
    status page's vendor-assigned *device slot number*; ``--index=T,E``
    addresses the 0-based *element index* within type descriptor T. SES-3
    gives no guarantee the two coincide (they do on the KTN-STL3; other
    shelves number slots 1-based or permuted), so every write translates
    through the enclosure's own AES-page mapping - and refuses outright when
    the mapping is absent or ambiguous, because lighting the WRONG bay's
    "pull this disk" LED is the worst failure this application can produce.
    """

    def __init__(self, backend: SysfsEnclosureBackend, ses: SesRunner) -> None:
        self.backend = backend
        self.ses = ses
        self._type_index: dict[str, tuple[int, int]] = {}
        #: logical id -> {device slot number -> per-type element index}, with
        #: None marking a slot that cannot be addressed unambiguously (claimed
        #: twice, claimed across bay-type blocks, or its block failed the
        #: element-numbering cross-check - see _build_slot_map for the full
        #: policy). Cached for the writer's lifetime, like
        #: _type_index: the slot->element wiring is chassis topology - which
        #: element addresses which physical bay is baked into the backplane
        #: and its firmware, and hot-swapping a drive changes which disk sits
        #: in a bay, never which element the bay answers to. Replacing the
        #: shelf itself changes the enclosure logical id, which is this
        #: cache's key, so even that cannot serve a stale map.
        self._slot_to_element: dict[str, dict[int, int | None]] = {}

    def _array_type_index(self, ref: object) -> tuple[int, int]:
        """Which SES type descriptor holds the drive bays, and how many.

        Returns (type index, declared element count), both read from the
        configuration page and cached per enclosure. The index is 0 on the
        KTN-STL3, but that is not guaranteed elsewhere. On modern shelves
        this is the "Array device slot" (0x17) descriptor; on older shelves
        array_slot_type_index() falls back to "Device slot" (0x01), whose
        bays the kernel ses driver exposes identically - everything
        downstream (IDENT addressing, the AES slot mapping) works on the
        type *index*, never the type name, so both kinds are handled the
        same way. The count is the AES integrity anchor: a bay block whose
        descriptor count disagrees with the configuration page has been
        shifted or truncated, and its positions cannot address anything.
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
            descriptors = parse_configuration(page.stdout)[1]
            index = array_slot_type_index(descriptors)
            if index is None:
                raise LocateError("enclosure reports no array device slot elements")
            self._type_index[logical_id] = (index, descriptors[index].count)
        return self._type_index[logical_id]

    def _slot_element_map(
        self, ref: object, type_index: int, expected_count: int
    ) -> dict[int, int | None]:
        """Device-slot-number -> element-index map for the bay type in use.

        Read from the additional element status page, because that page is the
        enclosure's own statement of which physical bay each element occupies;
        assuming identity instead is exactly the bug this method exists to
        prevent. Read once per enclosure logical id (see _slot_to_element for
        why the cache cannot go stale).
        """
        logical_id = ref.logical_id  # type: ignore[attr-defined]
        if logical_id not in self._slot_to_element:
            try:
                page = self.ses.read_page(
                    ref.sg_device,  # type: ignore[attr-defined]
                    "additional_element_status",
                )
            except SesError as exc:
                raise LocateError(
                    f"could not read SES additional element status: {exc}"
                ) from exc
            mapping = self._build_slot_map(
                parse_additional_element_status(page.stdout), type_index, expected_count
            )
            if not any(element is not None for element in mapping.values()):
                # Not cached, mirroring _array_type_index's raise-without-
                # cache: an anomalous one-off read (truncated output, a
                # firmware hiccup, a failed integrity check) must not pin
                # every future IDENT to refusal until restart. A genuinely
                # mapless enclosure just pays a page re-read per attempt.
                raise LocateError(
                    "refusing IDENT: the enclosure's additional element status "
                    "page maps no addressable device slot numbers for the bay "
                    "type in use (absent, ambiguous, or failed integrity checks)"
                )
            self._slot_to_element[logical_id] = mapping
        return self._slot_to_element[logical_id]

    @staticmethod
    def _build_slot_map(
        blocks: list, type_index: int, expected_count: int
    ) -> dict[int, int | None]:
        """Fold AES blocks into {device slot number -> per-type index}.

        The element index sg_ses needs is the entry's POSITION within its
        block, never the printed global index - but position is only
        trustworthy when the page passes every checkable invariant, because
        sg_ses's own print loop consumes descriptors positionally and can
        misattribute them when firmware omits an optional block or a
        descriptor. Refusal, never a guess, on any of:

        - **misattribution proof**: a ``device slot number`` inside a
          non-bay block. Expander/ESC/port descriptor formats never carry
          that field, so its presence means a bay descriptor was printed
          under the wrong header and every position on the page is suspect
          - the whole page is refused (empty map);
        - **count mismatch**: the chosen block's descriptor count differs
          from the configuration page's declared element count for that
          type descriptor (shifted, sparse, or truncated block);
        - **index/position offset drift**: within the chosen block, every
          printed index must sit at one constant offset from its list
          position (true under every EIIOE convention within one block,
          and undisturbed by interleaved eip=0 descriptors); an omitted or
          repeated descriptor shifts the offset mid-block. Vacuous for
          all-eip=0 blocks, which is what the count check is for;
        - **malformed block**: an unrecognised element header occupied a
          position (see AdditionalElementBlock.malformed);
        - a slot claimed twice, claimed by a second same-type-index block
          (a second subenclosure restarts positions), or claimed by the
          chosen block AND a foreign bay block - the kernel builds sysfs
          slots from every bay type, so the number cannot say which bay it
          means. A slot claimed only by a foreign bay block is left out
          entirely: refused as unmapped, it is not this descriptor's bay.
        """
        for block in blocks:
            if block.element_type in BAY_ELEMENT_TYPES:
                continue
            if any("device_slot_number" in entry for entry in block.entries):
                return {}

        first_chosen_seen = False
        claims: dict[int, list[tuple[bool, bool, int]]] = {}
        for block in blocks:
            if block.element_type not in BAY_ELEMENT_TYPES:
                continue
            same_ti = block.type_index == type_index
            chosen = same_ti and not first_chosen_seen
            if same_ti:
                first_chosen_seen = True

            # Printed indexes, where present, must sit at a single constant
            # offset from their position: one omitted or repeated descriptor
            # shifts everything after it, and the offset changing mid-block
            # is exactly that signature. Computed as a set difference so
            # interleaved eip=0 descriptors (no index at all) do not break
            # the invariant for their indexed neighbours.
            offsets = {
                e["element_index"] - position
                for position, e in enumerate(block.entries)
                if isinstance(e.get("element_index"), int)
            }
            trusted = (
                chosen
                and len(offsets) <= 1
                and not block.malformed
                and len(block.entries) == expected_count
            )

            for position, entry in enumerate(block.entries):
                slot_number = entry.get("device_slot_number")
                # An empty bay may carry no protocol descriptor and thus no
                # slot number; it stays unclaimed and IDENT for it is
                # refused rather than guessed at.
                if not isinstance(slot_number, int):
                    continue
                claims.setdefault(slot_number, []).append((same_ti, trusted, position))

        mapping: dict[int, int | None] = {}
        for slot_number, claim_list in claims.items():
            if len(claim_list) == 1 and claim_list[0][1]:
                mapping[slot_number] = claim_list[0][2]
            elif any(same_ti for same_ti, _, _ in claim_list) or len(claim_list) > 1:
                # Ambiguous or integrity-failed: recorded as None so the
                # refusal names the problem rather than reporting the slot
                # as unknown.
                mapping[slot_number] = None
            # A single claim from a foreign bay block: left out entirely,
            # refused as unmapped - it is not this type descriptor's bay.
        return mapping

    def _element_index_for(
        self, ref: object, type_index: int, expected_count: int, slot: int
    ) -> int:
        """Translate a sysfs slot number into the element index sg_ses needs.

        Refusal is deliberate policy: with no unambiguous mapping the only
        alternative is to pass the slot number through unmapped, and on a
        shelf where the two coordinate systems differ that lights the wrong
        bay - a wrong "pull this disk" indication. No LED at all is strictly
        safer than the wrong LED.
        """
        mapping = self._slot_element_map(ref, type_index, expected_count)
        element_index = mapping.get(slot)
        if element_index is not None:
            return element_index
        if slot in mapping:
            raise LocateError(
                f"refusing IDENT for slot {slot}: the enclosure's additional "
                f"element status page cannot address device slot number {slot} "
                "unambiguously (more than one element claims it, or its "
                "block's element numbering is inconsistent)"
            )
        offered = ", ".join(str(s) for s in sorted(mapping)) or "none"
        raise LocateError(
            f"refusing IDENT for slot {slot}: the enclosure's additional element "
            f"status page offers device slot numbers [{offered}], which do not "
            f"include {slot}"
        )

    def read(self, enclosure_id: str, slot: int) -> bool:
        enclosure_id, slot = validate_request(enclosure_id, slot)
        return self.backend.read_locate(self.backend.resolve(enclosure_id), slot)

    def write(self, enclosure_id: str, slot: int, on: bool) -> bool:
        enclosure_id, slot = validate_request(enclosure_id, slot)
        ref = self.backend.resolve(enclosure_id)
        type_index, expected_count = self._array_type_index(ref)
        element_index = self._element_index_for(ref, type_index, expected_count, slot)

        # Held across the command AND its settle poll, exactly as the sysfs
        # writer does. This is the default IDENT path, so without it the
        # write-atomicity guarantee applied only to the path almost nobody
        # uses: a concurrent slot sweep could sample the bay mid-flight and
        # cache a half-applied locate state for the UI to render.
        with enclosure_access(self.backend.lock_path):
            try:
                self.ses.set_ident(ref.sg_device, type_index, element_index, on)
            except SesError as exc:
                raise LocateError(f"IDENT command failed: {exc}") from exc

            # The settle read-back stays keyed by the sysfs slot: the kernel's
            # locate attribute lives under the slot directory, so it observes
            # the same physical bay the element index was derived from. On a
            # settle timeout the STALE value is returned rather than raising -
            # deliberately, because IdentManager.identify() compares the
            # returned state against the requested one, raises the
            # verification failure itself, and audits it; returning the
            # unsettled observation IS the failure signal.
            target = self.backend.slot_dir(ref, slot) / "locate"
            deadline = time.monotonic() + DEFAULT_SETTLE_TIMEOUT
            observed = self.backend.read_locate_at(target)
            while observed is not on and time.monotonic() < deadline:
                time.sleep(DEFAULT_SETTLE_POLL)
                observed = self.backend.read_locate_at(target)
        return observed


class HelperLocateWriter:
    """Talks to the privileged helper over a unix socket.

    The default timeout is sized to the helper's worst-case cold-cache write
    pipeline, not to a healthy round-trip: configuration-page read (<=20s,
    ses.DEFAULT_TIMEOUT) + AES-page read (<=20s) + enclosure flock wait
    (<=30s, access.DEFAULT_LOCK_TIMEOUT) + the IDENT command (<=20s) + the
    settle poll (<=2s). Giving up before the helper does is worse than
    waiting: the command may still complete server-side after the client
    stops listening, leaving a lit LED with no record of who owns it.
    """

    def __init__(self, socket_path: Path, timeout: float = 95.0) -> None:
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
