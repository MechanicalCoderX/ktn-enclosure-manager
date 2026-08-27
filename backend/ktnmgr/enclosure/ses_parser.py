"""Parsers for sg_ses configuration and joined status output.

Two facts about this output drive the design, both established by inspecting
real KTN-STL3 output rather than assumed:

1. In ``sg_ses --join`` the bracket pair is ``[type_descriptor_index,
   element_index]`` - the first number is NOT a subenclosure id. It runs 0..25
   on this shelf, across all five subenclosures. Mapping an element back to its
   subenclosure therefore requires the configuration page, which lists type
   descriptors in the same global order.

2. Element labels are not unique. Type descriptor 1 (LCC B) and type descriptor
   21 (PSU B) are both labelled ``Temp. Sensor B``. Elements are therefore keyed
   on ``(type_index, element_index)``, never on the label.

Values are parsed dynamically; none of the known-good readings in the spec are
hardcoded as expectations (§13).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ktnmgr.models import ChassisElement, ChassisTelemetry, Subenclosure

_SUBENC_RE = re.compile(r"^\s*Subenclosure identifier:\s*(\d+)")
_LOGICAL_ID_RE = re.compile(r"enclosure logical identifier \(hex\):\s*([0-9a-fA-F]+)")
_VENDOR_RE = re.compile(r"enclosure vendor:\s*(.*?)\s{2,}product:\s*(.*?)\s{2,}rev:\s*(\S*)")
_TYPEDESC_RE = re.compile(r"^\s*Element type:\s*(.+?),\s*subenclosure id:\s*(\d+)\s*$")
_COUNT_RE = re.compile(r"^\s*number of possible elements:\s*(\d+)")
_TEXT_RE = re.compile(r"^\s*text:\s*(.*)$")

_ELEMENT_RE = re.compile(r"^\[(-?\d+),(-?\d+)\]\s+Element type:\s*(.+?)\s*$")
# MULTILINE is required: the status line is rarely the last line of an element
# block (temperature, rpm and phy tables follow it), so a bare '$' anchor
# silently matched almost nothing and left every element as 'unknown'.
_STATUS_RE = re.compile(r"status:\s*([A-Za-z][A-Za-z ./-]*?)\s*$", re.MULTILINE)
_KV_RE = re.compile(r"([A-Za-z][A-Za-z0-9 ./_-]*?)\s*=\s*([^,]+)")
_TEMP_RE = re.compile(r"Temperature\s*=\s*(-?\d+)\s*C")
_RPM_RE = re.compile(r"Actual speed\s*=\s*(\d+)\s*rpm")
_OVERALL_RE = re.compile(
    r"INVOP=(\d+).*?INFO=(\d+).*?NON-CRIT=(\d+).*?CRIT=(\d+).*?UNRECOV=(\d+)", re.DOTALL
)

# Additional element status (sg_ses -p aes). Unlike the configuration page,
# each type header here carries its own [ti=N], so no cross-reference against
# the configuration order is needed to recover the type index.
_AES_TYPEDESC_RE = re.compile(
    r"^\s*Element type:\s*(.+?),\s*subenclosure id:\s*(\d+)\s*\[ti=(\d+)\]\s*$"
)
_AES_ELEMENT_RE = re.compile(r"^\s*Element index:\s*(\d+)(?:\s+eiioe=(\d+))?\s*$")
# eip=0 descriptors carry no element index field; sg_ses then prints
# "Element %d descriptor" instead (the else-branch of the eip test in
# sg_ses.c). The number is sg_ses's own running count, not an index into
# anything, so it is not captured - the descriptor's position in the block
# is what identifies the element (see parse_additional_element_status).
_AES_ELEMENT_NOIDX_RE = re.compile(r"^\s*Element\s+\d+\s+descriptor\s*$")
# A type header line that failed the full header regex (typically missing
# its [ti=N]): the block it opens cannot be identified, so everything under
# it must be dropped rather than attributed to the previous block.
_AES_TYPEHDR_GUARD_RE = re.compile(r"^\s*Element type:")
# Any other line that opens with "Element" at descriptor indentation is an
# element header this parser does not understand; it occupied a position, so
# the enclosing block's positions become untrustworthy (malformed).
_AES_ELEMENT_GUARD_RE = re.compile(r"^\s*Element\b")
_AES_SLOT_RE = re.compile(r"device slot number:\s*(\d+)")
# Anchoring at line start (after indentation) is what excludes the expander's
# "attached SAS address:" line -- only the drive's own address may match.
_AES_SAS_ADDR_RE = re.compile(r"^\s*SAS address:\s*(0x[0-9a-fA-F]+)\s*$")


class TypeDescriptor:
    """One entry of the configuration page's type descriptor list."""

    __slots__ = ("index", "subenclosure_id", "element_type", "label", "count")

    def __init__(
        self, index: int, subenclosure_id: int, element_type: str, label: str, count: int
    ) -> None:
        self.index = index
        self.subenclosure_id = subenclosure_id
        self.element_type = element_type
        self.label = label
        self.count = count

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TypeDescriptor(index={self.index}, sub={self.subenclosure_id}, "
            f"type={self.element_type!r}, label={self.label!r}, n={self.count})"
        )


def parse_configuration(text: str) -> tuple[list[Subenclosure], list[TypeDescriptor]]:
    """Parse ``sg_ses -p cf`` into subenclosures and the ordered type list.

    The type descriptor list order is the join output's first index, so this
    must preserve file order exactly.
    """
    lines = text.splitlines()
    subenclosures: list[Subenclosure] = []
    descriptors: list[TypeDescriptor] = []

    current_sub: int | None = None
    pending: dict[str, str] = {}

    for index, line in enumerate(lines):
        sub_match = _SUBENC_RE.match(line)
        if sub_match:
            _flush_subenclosure(subenclosures, current_sub, pending)
            current_sub = int(sub_match.group(1))
            pending = {}
            continue

        if current_sub is not None:
            logical = _LOGICAL_ID_RE.search(line)
            if logical:
                pending["logical_id"] = logical.group(1).lower()
            vendor = _VENDOR_RE.search(line)
            if vendor:
                pending["vendor"] = vendor.group(1).strip()
                pending["product"] = vendor.group(2).strip()
                pending["revision"] = vendor.group(3).strip()

        type_match = _TYPEDESC_RE.match(line)
        if type_match:
            _flush_subenclosure(subenclosures, current_sub, pending)
            current_sub = None
            pending = {}

            count = 0
            label = ""
            seen_count = False
            for lookahead in lines[index + 1 : index + 5]:
                # sg_ses prints the "text:" line only when the descriptor's
                # text length is nonzero (if(txt_len) in sg_ses.c), so a
                # textless descriptor is normal SES output, not truncation.
                # Without this boundary the fixed window would run into the
                # NEXT "Element type:" block and steal its count and label.
                if _TYPEDESC_RE.match(lookahead):
                    break
                count_match = _COUNT_RE.match(lookahead)
                if count_match and not seen_count:
                    count = int(count_match.group(1))
                    seen_count = True
                text_match = _TEXT_RE.match(lookahead)
                if text_match:
                    label = text_match.group(1).strip()
                    break
            descriptors.append(
                TypeDescriptor(
                    index=len(descriptors),
                    subenclosure_id=int(type_match.group(2)),
                    element_type=type_match.group(1).strip(),
                    label=label,
                    count=count,
                )
            )

    _flush_subenclosure(subenclosures, current_sub, pending)
    return subenclosures, descriptors


def _flush_subenclosure(
    into: list[Subenclosure], sub_id: int | None, pending: dict[str, str]
) -> None:
    if sub_id is None or not pending:
        return
    if any(s.subenclosure_id == sub_id for s in into):
        return
    into.append(
        Subenclosure(
            subenclosure_id=sub_id,
            vendor=pending.get("vendor", ""),
            product=pending.get("product", ""),
            revision=pending.get("revision", ""),
            logical_id=pending.get("logical_id"),
        )
    )


def parse_join(text: str, descriptors: list[TypeDescriptor]) -> list[ChassisElement]:
    """Parse ``sg_ses --join`` output into structured elements."""
    elements: list[ChassisElement] = []
    current: ChassisElement | None = None
    body: list[str] = []

    for line in text.splitlines():
        match = _ELEMENT_RE.match(line)
        if match:
            if current is not None:
                _finalise(current, body)
                elements.append(current)
            type_index = int(match.group(1))
            element_index = int(match.group(2))
            descriptor = (
                descriptors[type_index] if 0 <= type_index < len(descriptors) else None
            )
            # element_index < 0 marks sg_ses's "Overall descriptor" -- the
            # per-element-type summary rather than a physical sensor.
            #
            # DO NOT surface these as readings. On this KTN-STL3 they actively
            # disagree with the real per-element values (measured 2026-08-18):
            #
            #   ti=18 overall = 30 C  but its only element 0 = 22 C
            #   ti=21 overall = 22 C  but elements 0/1 = 29 C / 22 C
            #
            # 22 C is the correct inlet temperature -- corroborated by all 15
            # drives sitting at 32-34 C, which implies a ~22 C inlet given the
            # normal 10-11 C rise. A 30 C ambient would put them at 40-42 C.
            # Consumers filter on is_overall; see views.tsx and services/state.py.
            current = ChassisElement(
                type_index=type_index,
                element_index=element_index,
                subenclosure_id=descriptor.subenclosure_id if descriptor else -1,
                element_type=match.group(3).strip(),
                label=descriptor.label if descriptor else match.group(3).strip(),
                status="unknown",
                is_overall=element_index < 0,
            )
            body = []
            continue
        if current is not None:
            body.append(line)

    if current is not None:
        _finalise(current, body)
        elements.append(current)

    return elements


def _finalise(element: ChassisElement, body: list[str]) -> None:
    blob = "\n".join(body)

    status = _STATUS_RE.search(blob)
    if status:
        element.status = status.group(1).strip()

    temperature = _TEMP_RE.search(blob)
    if temperature:
        element.temperature_c = float(temperature.group(1))

    rpm = _RPM_RE.search(blob)
    if rpm:
        element.speed_rpm = int(rpm.group(1))

    for line in body:
        stripped = line.strip()
        # The element's own SAS address is worth keeping; the attached address
        # belongs to the expander, not the drive, so it is skipped.
        if stripped.startswith("SAS address:"):
            element.fields.setdefault("SAS address", stripped.split(":", 1)[1].strip())
            continue
        # Otherwise skip the phy/connector tables, which are free-form and
        # handled separately by the SAS advanced view.
        if stripped.startswith(("[", "phy index", "attached", "SAS ", "Transport")):
            continue
        for key, value in _KV_RE.findall(line):
            cleaned = value.strip().rstrip(",")
            if cleaned:
                element.fields[key.strip()] = cleaned


def parse_overall_flags(text: str) -> dict[str, int]:
    """Extract the INVOP/INFO/NON-CRIT/CRIT/UNRECOV summary if present."""
    match = _OVERALL_RE.search(text)
    if not match:
        return {}
    keys = ("INVOP", "INFO", "NON-CRIT", "CRIT", "UNRECOV")
    return {key: int(value) for key, value in zip(keys, match.groups(), strict=True)}


def build_telemetry(
    enclosure_id: str, configuration_text: str, join_text: str
) -> ChassisTelemetry:
    """Compose a full chassis telemetry snapshot from two page reads."""
    subenclosures, descriptors = parse_configuration(configuration_text)
    elements = parse_join(join_text, descriptors)
    return ChassisTelemetry(
        enclosure_id=enclosure_id,
        subenclosures=subenclosures,
        elements=elements,
        overall_flags=parse_overall_flags(join_text) or parse_overall_flags(configuration_text),
        collected_at=datetime.now(UTC),
    )


#: SES element type name for a drive bay (SES type 0x17), as sg_ses prints it.
ARRAY_DEVICE_SLOT = "Array device slot"
#: Older shelves report bays as SES type 0x01, which sg_ses prints as
#: "Device slot". The kernel ses driver creates sysfs slots for both types,
#: so bay filtering must accept both names or those shelves show zero bays.
DEVICE_SLOT = "Device slot"
#: Every element-type name that means "a drive bay". Order matters: 0x17 is
#: the modern type and wins when a shelf exposes both (see
#: array_slot_type_index).
BAY_ELEMENT_TYPES = (ARRAY_DEVICE_SLOT, DEVICE_SLOT)


def array_slot_type_index(descriptors: list[TypeDescriptor]) -> int | None:
    """Find the type descriptor index that holds the drive bays.

    ``sg_ses --index=T,E`` addresses element E of type descriptor T, so IDENT
    needs T. It is 0 on the KTN-STL3 but that is not guaranteed on other
    shelves, so it is discovered from the configuration page rather than
    assumed. Both bay type names are accepted; when a shelf exposes both,
    "Array device slot" (0x17) wins because it is the type the SES-3 spec
    designates for individually addressable drive bays.
    """
    device_slot_fallback: int | None = None
    for descriptor in descriptors:
        if descriptor.count <= 0:
            continue
        if descriptor.element_type == ARRAY_DEVICE_SLOT:
            return descriptor.index
        if descriptor.element_type == DEVICE_SLOT and device_slot_fallback is None:
            device_slot_fallback = descriptor.index
    return device_slot_fallback


class AdditionalElementBlock:
    """One block of the additional element status page, any eligible type.

    ``entries`` is the block's descriptors in file order, one dict per
    element. For a bay-type block, **the list position is the element's
    0-based index within its type descriptor** - the coordinate ``sg_ses
    --index=T,E`` addresses - PROVIDED the block passes the integrity checks
    the consumers apply. The identification is positional because SES-3
    returns AES descriptors in status-page element order and sg_ses prints
    them in that order (covering eip=0 descriptors, which carry no index
    field, and every EIIOE convention). It is NOT unconditional: AES
    descriptors are optional for three eligible types (the SCSI port types
    and ESC electronics), and sg_ses's positional print loop can misattribute
    descriptors when a block is omitted or sparse - which is why non-bay
    blocks are returned too (a ``device slot number`` appearing under one is
    proof of misattribution), why ``malformed`` exists, and why consumers
    must cross-check printed indexes and element counts and REFUSE on any
    inconsistency rather than trust position alone.

    The printed ``ELEMENT INDEX`` field, when present, is kept as
    ``element_index`` **for cross-checking only**: it is a global index
    across type descriptors (the KTN-STL3 capture proves it - the ti=4
    expander prints 42, the sum of the four preceding types' counts), whose
    zero point additionally shifts with EIIOE. Feeding it to ``--index`` is
    exactly the wrong-bay bug the AES mapping exists to prevent.

    Other fields per entry: ``device_slot_number`` (int), ``sas_address``
    (str, the drive's own address as printed, e.g. ``0x5000cca0e0000002``)
    and ``eiioe`` (int); a field the output did not carry is simply absent.
    """

    __slots__ = ("element_type", "subenclosure_id", "type_index", "entries", "malformed")

    def __init__(self, element_type: str, subenclosure_id: int, type_index: int) -> None:
        self.element_type = element_type
        self.subenclosure_id = subenclosure_id
        self.type_index = type_index
        self.entries: list[dict[str, int | str]] = []
        #: True when a line inside this block looked like an element header
        #: but matched no known form: an uncounted element means every later
        #: position may be shifted, so the whole block's positions are
        #: untrustworthy and consumers must refuse its claims.
        self.malformed: bool = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AdditionalElementBlock(type={self.element_type!r}, "
            f"sub={self.subenclosure_id}, ti={self.type_index}, "
            f"n={len(self.entries)}, malformed={self.malformed})"
        )


def parse_additional_element_status(text: str) -> list[AdditionalElementBlock]:
    """Parse ``sg_ses -p aes`` into per-block element lists.

    EVERY block with a well-formed type header is returned, not just the
    bay-type ones: sg_ses's positional print loop can attribute a bay
    descriptor to a preceding non-bay block when firmware omits an optional
    block, and the tell-tale - a ``device slot number`` line under an
    expander/ESC/port header, whose descriptor formats never carry that
    field - is only visible if the non-bay blocks are parsed too. Consumers
    filter to BAY_ELEMENT_TYPES for addressing and use the rest for
    integrity checking.

    The page exists because element index and physical bay are not the same
    thing: the ``device slot number`` field is the enclosure's own statement
    of which physical bay an element occupies. On the KTN-STL3 the two happen
    to be equal for all 15 bays, but SES does not guarantee that (other
    shelves number slots 1-based or permuted), so consumers must map through
    this page rather than assume identity.
    """
    blocks: list[AdditionalElementBlock] = []
    current_block: AdditionalElementBlock | None = None
    current_entry: dict[str, int | str] | None = None

    for line in text.splitlines():
        header = _AES_TYPEDESC_RE.match(line)
        if header:
            current_entry = None
            current_block = AdditionalElementBlock(
                element_type=header.group(1).strip(),
                subenclosure_id=int(header.group(2)),
                type_index=int(header.group(3)),
            )
            blocks.append(current_block)
            continue
        if _AES_TYPEHDR_GUARD_RE.match(line):
            # A type header missing its [ti=N] (or otherwise unparseable):
            # the block it opens cannot be identified, so its descriptors
            # must be dropped entirely - attributing them to the PREVIOUS
            # block would shift that block's positions, a wrong bay
            # downstream.
            current_entry = None
            current_block = None
            continue

        if current_block is None:
            continue

        element = _AES_ELEMENT_RE.match(line)
        if element:
            current_entry = {"element_index": int(element.group(1))}
            if element.group(2) is not None:
                current_entry["eiioe"] = int(element.group(2))
            current_block.entries.append(current_entry)
            continue
        if _AES_ELEMENT_NOIDX_RE.match(line):
            # eip=0: no index field exists; the position appended here is the
            # element's whole identity.
            current_entry = {}
            current_block.entries.append(current_entry)
            continue
        if _AES_ELEMENT_GUARD_RE.match(line):
            # An element header this parser does not recognise. It still
            # occupied a position in the block, so silently dropping it
            # would shift every later element's position - the block's
            # positions can no longer be trusted at all, which the
            # malformed flag tells consumers to refuse on.
            current_entry = None
            current_block.malformed = True
            continue

        if current_entry is None:
            continue
        slot_match = _AES_SLOT_RE.search(line)
        if slot_match:
            current_entry.setdefault("device_slot_number", int(slot_match.group(1)))
        addr_match = _AES_SAS_ADDR_RE.match(line)
        if addr_match:
            current_entry.setdefault("sas_address", addr_match.group(1))

    return blocks
