"""Domain models.

Identity rule (spec §20): a drive is never keyed by /dev/sdX. The persistent
key is (enclosure logical id, SES slot); the disk occupying it is identified by
serial + WWN. /dev/sdX is runtime-only information that may change across boots
or HBA resets.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class SlotHealth(StrEnum):
    """Health of a bay. Never communicated by colour alone in the UI (§24)."""

    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class ZfsState(StrEnum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    FAULTED = "FAULTED"
    OFFLINE = "OFFLINE"
    UNAVAIL = "UNAVAIL"
    REMOVED = "REMOVED"
    SPARE = "SPARE"
    UNKNOWN = "UNKNOWN"


class EnclosureRef(BaseModel):
    """A discovered SES enclosure, identified by attributes rather than path."""

    logical_id: str = Field(description="Enclosure logical identifier, e.g. 0x50060480aabbcc00")
    vendor: str
    product: str
    revision: str
    scsi_address: str = Field(description="e.g. 1:0:15:0 - runtime only, may change")
    sysfs_path: str = Field(description="runtime only, may change")
    sg_device: str | None = Field(default=None, description="e.g. /dev/sg16 - runtime only")
    bsg_device: str | None = Field(default=None, description="e.g. /dev/bsg/1:0:15:0")
    slot_count: int

    @property
    def key(self) -> str:
        """Stable identity used in API paths and the audit log."""
        return self.logical_id


class SlotState(BaseModel):
    """Raw per-bay state read from the Linux enclosure sysfs ABI."""

    ses_slot: int
    display_bay: int = Field(description="1-based, left to right; = ses_slot + 1 on KTN-STL3")
    status: str = Field(default="unknown", description="verbatim sysfs status, e.g. OK")
    power_status: str | None = None
    locate: bool = False
    fault: bool = False
    active: bool | None = None
    block_device: str | None = Field(default=None, description="e.g. sdb - runtime only")
    sysfs_path: str


class DiskIdentity(BaseModel):
    """Stable disk identity, correlated to a slot via sysfs device/block."""

    serial: str | None = None
    wwn: str | None = None
    model: str | None = None
    firmware: str | None = None
    size_bytes: int | None = None
    sas_address: str | None = Field(
        default=None,
        description=(
            "From SES additional element status. NOTE: this is the SAS *port* address and "
            "differs from the block layer's node WWN (observed offset of 2 on this hardware), "
            "so it must never be used to correlate SES slots to disks by equality."
        ),
    )
    transport: str | None = None
    rotational: bool | None = None


class ZfsInfo(BaseModel):
    pool: str | None = None
    vdev: str | None = None
    state: ZfsState = ZfsState.UNKNOWN
    read_errors: int | None = None
    write_errors: int | None = None
    checksum_errors: int | None = None
    is_spare: bool = False
    resilvering: bool = False


class SmartInfo(BaseModel):
    """Disk health as far as the TrueNAS API can actually report it.

    There is deliberately no ``overall`` or ``power_on_hours`` field. Both used
    to exist and were permanently ``None``: the 25.10 API exposes no SMART
    overall status and no power-on hours, so nothing could ever fill them, and
    the UI rendered two blanks that read as missing data rather than as absent
    capability. Populating them would mean shelling out to smartctl against
    every disk, which this application deliberately does not do.

    What the API *does* expose is TrueNAS' own temperature alerting, which is a
    real health signal, so that is carried here instead.
    """

    temperature_c: float | None = None
    available: bool = False
    over_temperature: bool = False
    alert: str | None = None


class Bay(BaseModel):
    """A composed view of one physical bay: sysfs + disk + TrueNAS/ZFS + SMART."""

    display_bay: int
    ses_slot: int
    enclosure_id: str
    device: str | None = None
    health: SlotHealth = SlotHealth.UNKNOWN
    status: str = "unknown"
    power_status: str | None = None
    locate: bool = False
    fault: bool = False
    ident_expires_at: datetime | None = None
    ident_origin: str | None = Field(
        default=None, description="'app' or 'external' when locate is on (§27)"
    )
    disk: DiskIdentity = Field(default_factory=DiskIdentity)
    zfs: ZfsInfo = Field(default_factory=ZfsInfo)
    smart: SmartInfo = Field(default_factory=SmartInfo)
    sysfs_path: str | None = None


class ChassisElement(BaseModel):
    """One SES element, keyed by (type_index, element_index).

    Keying on the device-supplied text label alone is ambiguous: on the KTN-STL3
    both type descriptor 1 (LCC B) and 21 (PSU B) are labelled 'Temp. Sensor B'.
    """

    type_index: int
    element_index: int
    subenclosure_id: int
    element_type: str
    label: str
    status: str
    is_overall: bool = False
    fields: dict[str, str] = Field(default_factory=dict)
    temperature_c: float | None = None
    speed_rpm: int | None = None
    # A Cooling status descriptor carries three independent facts, and rpm alone
    # is the least informative of them: this shelf's firmware modulates its fans
    # on its own, so the operator needs to see WHICH speed step the enclosure has
    # chosen and whether it has been asked to run at all. All three are read here
    # and never written - chassis management stays read-only (§15).
    speed_code: int | None = Field(
        default=None,
        ge=0,
        le=7,
        description=(
            "SES-3 ACTUAL SPEED CODE: 0 = fan stopped, 1..6 = lowest through second "
            "highest, 7 = highest. None when the output printed no speed phrase or a "
            "phrase this parser does not map. Never defaulted: on the status page 0 is "
            "a real reading meaning the fan has stopped, so a default would be an alarm "
            "the enclosure never raised. (The identically numbered control-page field "
            "means 'leave at current speed' at 0; the two tables are not interchangeable "
            "and this field is only ever the status one.)"
        ),
    )
    speed_phrase: str | None = Field(
        default=None,
        description=(
            "The speed wording exactly as printed, e.g. 'Fan at third lowest speed'. "
            "Kept verbatim alongside speed_code so wording this parser cannot map is "
            "still shown rather than lost; None when no phrase was printed."
        ),
    )
    requested_on: bool | None = Field(
        default=None,
        description=(
            "SES-3 RQSTED ON bit, printed for Cooling and Power supply elements. None "
            "when the element's output did not carry it - absent is not False (§13)."
        ),
    )


class Subenclosure(BaseModel):
    subenclosure_id: int
    vendor: str
    product: str
    revision: str
    logical_id: str | None = None


class ChassisTelemetry(BaseModel):
    enclosure_id: str
    subenclosures: list[Subenclosure] = Field(default_factory=list)
    elements: list[ChassisElement] = Field(default_factory=list)
    overall_flags: dict[str, int] = Field(default_factory=dict)
    collected_at: datetime | None = None
    stale: bool = False
    error: str | None = None


class AuditEntry(BaseModel):
    timestamp: datetime
    user: str
    enclosure: str
    bay: int | None = None
    ses_slot: int | None = None
    serial: str | None = None
    operation: str
    previous: str | None = None
    result: str | None = None
    verification: str
    detail: str | None = None
