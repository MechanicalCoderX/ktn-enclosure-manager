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
            for lookahead in lines[index + 1 : index + 5]:
                count_match = _COUNT_RE.match(lookahead)
                if count_match:
                    count = int(count_match.group(1))
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
        # Only parse the flag lines, not the phy/connector tables, which are
        # free-form and handled separately by the SAS advanced view.
        if line.strip().startswith(("[", "phy index", "attached", "SAS ", "Transport")):
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


#: SES element type name for a drive bay, as sg_ses prints it.
ARRAY_DEVICE_SLOT = "Array device slot"


def array_slot_type_index(descriptors: list[TypeDescriptor]) -> int | None:
    """Find the type descriptor index that holds the drive bays.

    ``sg_ses --index=T,E`` addresses element E of type descriptor T, so IDENT
    needs T. It is 0 on the KTN-STL3 but that is not guaranteed on other
    shelves, so it is discovered from the configuration page rather than
    assumed.
    """
    for descriptor in descriptors:
        if descriptor.element_type == ARRAY_DEVICE_SLOT and descriptor.count > 0:
            return descriptor.index
    return None
