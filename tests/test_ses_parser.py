"""SES parser tests against captured KTN-STL3 output (spec §41, §42)."""

from __future__ import annotations

from pathlib import Path

import pytest
from ktnmgr.enclosure.ses_parser import (
    BAY_ELEMENT_TYPES,
    array_slot_type_index,
    build_telemetry,
    parse_additional_element_status,
    parse_configuration,
    parse_join,
    parse_overall_flags,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ktn-stl3"
# Hand-written variants of sg_ses output the KTN-STL3 does not produce, so the
# parsers are exercised on SES shapes seen on other shelves (textless type
# descriptors, 'Device slot' bays, permuted slot numbering).
SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture(scope="module")
def configuration_text() -> str:
    return (FIXTURES / "sg_cf.txt").read_text()


@pytest.fixture(scope="module")
def join_text() -> str:
    return (FIXTURES / "sg_join_unfiltered.txt").read_text()


# ---------------------------------------------------------------- configuration


def test_five_subenclosures(configuration_text: str) -> None:
    subs, _ = parse_configuration(configuration_text)
    assert [s.subenclosure_id for s in subs] == [0, 1, 2, 3, 4]


def test_subenclosure_products(configuration_text: str) -> None:
    subs, _ = parse_configuration(configuration_text)
    by_id = {s.subenclosure_id: s for s in subs}
    assert by_id[0].product == "Viper LCC"
    assert by_id[0].vendor == "EMC"
    assert by_id[0].revision == "0B70"
    assert by_id[0].logical_id == "50060480aabbcc00"
    assert by_id[1].logical_id == "50060480aabbcc10"
    assert by_id[2].product == "Viper Encl"


def test_twenty_six_type_descriptors(configuration_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    assert len(descriptors) == 26
    assert [d.index for d in descriptors] == list(range(26))


def test_type_descriptor_details(configuration_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    assert descriptors[0].element_type == "Array device slot"
    assert descriptors[0].label == "Array Device"
    assert descriptors[0].count == 15
    assert descriptors[0].subenclosure_id == 0

    assert descriptors[13].label == "Expander A"
    assert descriptors[13].subenclosure_id == 1

    assert descriptors[25].label == "Power Supply A"
    assert descriptors[25].subenclosure_id == 4


def test_ambiguous_labels_are_disambiguated_by_index(configuration_text: str) -> None:
    """The whole reason elements are keyed on (type_index, element_index):
    two different type descriptors carry the identical label 'Temp. Sensor B'."""
    _, descriptors = parse_configuration(configuration_text)
    assert descriptors[1].label == "Temp. Sensor B"
    assert descriptors[21].label == "Temp. Sensor B"
    assert descriptors[1].subenclosure_id == 0  # LCC B
    assert descriptors[21].subenclosure_id == 3  # PSU B
    assert descriptors[1].subenclosure_id != descriptors[21].subenclosure_id


def test_textless_descriptor_does_not_steal_next_descriptors_fields() -> None:
    """sg_ses prints the 'text:' line only when the descriptor's text length
    is nonzero (if(txt_len) in sg_ses.c), so a textless descriptor is a
    normal SES variant. A fixed lookahead window that does not stop at the
    next 'Element type:' line takes THAT descriptor's count and label."""
    text = (SYNTHETIC / "sg_cf_textless.txt").read_text()
    _, descriptors = parse_configuration(text)
    assert [d.element_type for d in descriptors] == [
        "Array device slot",
        "Temperature sensor",
        "Cooling",
        "Power supply",
    ]
    # Descriptor 0 is textless: it must keep its own count and an empty
    # label, not inherit 2/'Temp Sensor' from the descriptor after it.
    assert descriptors[0].count == 12
    assert descriptors[0].label == ""
    assert descriptors[1].count == 2
    assert descriptors[1].label == "Temp Sensor"
    # The same shape again mid-list: textless Cooling before Power supply.
    assert descriptors[2].count == 3
    assert descriptors[2].label == ""
    assert descriptors[3].count == 2
    assert descriptors[3].label == "Power Supply"


# --------------------------------------------------------------- bay discovery


def test_array_slot_type_index_on_real_capture(configuration_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    assert array_slot_type_index(descriptors) == 0


def test_device_slot_bays_are_recognised() -> None:
    """Older shelves report bays as SES type 0x01, printed by sg_ses as
    'Device slot' rather than 'Array device slot' (0x17). The kernel ses
    driver creates sysfs slots for both, so bay discovery must accept both
    or those shelves show zero bays."""
    text = (SYNTHETIC / "sg_cf_device_slot.txt").read_text()
    _, descriptors = parse_configuration(text)
    assert descriptors[0].element_type == "Device slot"
    assert descriptors[0].count == 12
    assert array_slot_type_index(descriptors) == 0


def test_array_device_slot_preferred_over_device_slot() -> None:
    """When a shelf exposes both bay types, 0x17 wins regardless of file
    order: it is the type SES-3 designates for addressable drive bays."""
    text = (
        "  type descriptor header and text list\n"
        "    Element type: Device slot, subenclosure id: 0\n"
        "      number of possible elements: 4\n"
        "      text: Legacy Bays\n"
        "    Element type: Array device slot, subenclosure id: 0\n"
        "      number of possible elements: 12\n"
        "      text: Array Device\n"
    )
    _, descriptors = parse_configuration(text)
    assert array_slot_type_index(descriptors) == 1


# ------------------------------------------------------------------------ join


def test_join_element_count(configuration_text: str, join_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    elements = parse_join(join_text, descriptors)
    overall = [e for e in elements if e.is_overall]
    individual = [e for e in elements if not e.is_overall]
    assert len(overall) == 26
    assert len(individual) == 108


def test_join_attributes_subenclosure_via_configuration(
    configuration_text: str, join_text: str
) -> None:
    """The join output's first bracket field is a type index, not a subenclosure
    id; correct attribution is only possible by cross-referencing the config."""
    _, descriptors = parse_configuration(configuration_text)
    by_key = {(e.type_index, e.element_index): e for e in parse_join(join_text, descriptors)}

    lcc_b_temp = by_key[(1, 0)]
    psu_b_temp = by_key[(21, 0)]
    assert lcc_b_temp.subenclosure_id == 0
    assert psu_b_temp.subenclosure_id == 3
    assert lcc_b_temp.label == psu_b_temp.label == "Temp. Sensor B"


def test_temperature_parsed(configuration_text: str, join_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    by_key = {(e.type_index, e.element_index): e for e in parse_join(join_text, descriptors)}
    assert by_key[(1, 0)].temperature_c == 31.0
    assert by_key[(1, 0)].element_type == "Temperature sensor"


def test_fan_rpm_parsed(configuration_text: str, join_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    by_key = {(e.type_index, e.element_index): e for e in parse_join(join_text, descriptors)}
    fan = by_key[(20, 0)]
    assert fan.element_type == "Cooling"
    assert fan.speed_rpm == 5300
    assert fan.label == "Cooling Fan B"


def test_power_supply_flags_parsed(configuration_text: str, join_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    by_key = {(e.type_index, e.element_index): e for e in parse_join(join_text, descriptors)}
    psu = by_key[(22, 0)]
    assert psu.element_type == "Power supply"
    assert psu.status == "OK"
    for flag in ("AC fail", "DC fail", "DC overvoltage", "DC undervoltage", "Fail"):
        assert psu.fields[flag] == "0", f"{flag} should be 0 in the healthy baseline"


def test_expander_and_controller_present(configuration_text: str, join_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    elements = parse_join(join_text, descriptors)
    expanders = [e for e in elements if e.element_type == "SAS expander" and not e.is_overall]
    controllers = [
        e
        for e in elements
        if e.element_type == "Enclosure services controller electronics" and not e.is_overall
    ]
    assert {e.label for e in expanders} == {"Expander A", "Expander B"}
    assert {e.label for e in controllers} == {"Controller A", "Controller B"}


def test_all_array_slots_ok(configuration_text: str, join_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    slots = [
        e
        for e in parse_join(join_text, descriptors)
        if e.element_type == "Array device slot" and not e.is_overall
    ]
    assert len(slots) == 15
    assert all(s.status == "OK" for s in slots)


def test_build_telemetry_end_to_end(configuration_text: str, join_text: str) -> None:
    telemetry = build_telemetry("0x50060480aabbcc00", configuration_text, join_text)
    assert telemetry.enclosure_id == "0x50060480aabbcc00"
    assert len(telemetry.subenclosures) == 5
    assert len(telemetry.elements) == 134
    assert telemetry.collected_at is not None
    assert telemetry.error is None


# -------------------------------------------------------------- robustness §37


def test_malformed_output_does_not_raise() -> None:
    subs, descriptors = parse_configuration("total garbage\nnot ses output\n")
    assert subs == []
    assert descriptors == []
    assert parse_join("[nonsense\n", descriptors) == []


def test_truncated_join_keeps_what_it_parsed(configuration_text: str) -> None:
    _, descriptors = parse_configuration(configuration_text)
    truncated = "[1,0]  Element type: Temperature sensor\n  Enclosure Status:\n"
    elements = parse_join(truncated, descriptors)
    assert len(elements) == 1
    assert elements[0].status == "unknown"


def test_overall_flags_absent_returns_empty() -> None:
    assert parse_overall_flags("nothing here") == {}


# --------------------------------- additional element status (sg_ses -p aes)


@pytest.fixture(scope="module")
def aes_text() -> str:
    return (FIXTURES / "sg_aes.txt").read_text()


def _bays(blocks: list) -> list:
    return [b for b in blocks if b.element_type in BAY_ELEMENT_TYPES]


def test_aes_returns_every_block_for_integrity_checking(aes_text: str) -> None:
    """All eligible blocks come back, not just the bays: a device slot number
    under a non-bay block is the consumers' proof of sg_ses misattribution
    (an omitted optional AES block shifting the positional print), which is
    only visible if the non-bay blocks are parsed too."""
    blocks = parse_additional_element_status(aes_text)
    bays = [b for b in blocks if b.element_type in BAY_ELEMENT_TYPES]
    assert len(bays) == 1
    assert (bays[0].element_type, bays[0].subenclosure_id, bays[0].type_index) == (
        "Array device slot", 0, 0,
    )
    # The capture's expander block is returned too, and its descriptors must
    # carry no device slot number (the misattribution tell-tale).
    others = [b for b in blocks if b.element_type not in BAY_ELEMENT_TYPES]
    assert others, "the capture carries a SAS expander block"
    for block in others:
        assert all("device_slot_number" not in e for e in block.entries)


def test_aes_slot_identity_on_ktn_stl3(aes_text: str) -> None:
    """On the KTN-STL3 the printed index, the list position and the device
    slot number all coincide for the 15 bays. That identity is a property of
    THIS shelf (its bay descriptor is first, eiioe=0, slots numbered from
    zero), not of SES -- which is why the mapping must be positional and read
    from the page instead of assumed (the fixtures below are the
    counterexamples)."""
    (block,) = _bays(parse_additional_element_status(aes_text))
    assert len(block.entries) == 15
    for position, entry in enumerate(block.entries):
        assert entry["element_index"] == position
        assert entry["device_slot_number"] == position
        assert entry["eiioe"] == 0


def test_aes_takes_drive_address_not_attached(aes_text: str) -> None:
    (block,) = _bays(parse_additional_element_status(aes_text))
    addresses = {entry["sas_address"] for entry in block.entries}
    assert len(addresses) == 15
    # 0x50060480aabbcc01 is the expander the drives attach through; a parser
    # that matched 'attached SAS address' would return it for all 15 bays.
    assert "0x50060480aabbcc01" not in addresses
    assert block.entries[0]["sas_address"] == "0x5000cca0e0000002"


def test_aes_permuted_slot_numbers() -> None:
    """Synthetic shelf whose slot numbering is 1-based and permuted relative
    to element positions, with one empty bay carrying no phy list at all."""
    text = (SYNTHETIC / "sg_aes_permuted.txt").read_text()
    (block,) = _bays(parse_additional_element_status(text))
    assert block.element_type == "Device slot"
    assert block.subenclosure_id == 0
    assert block.type_index == 0
    assert [e["element_index"] for e in block.entries] == [0, 1, 2, 3]
    assert block.entries[0] == {
        "element_index": 0,
        "eiioe": 0,
        "device_slot_number": 4,
        "sas_address": "0x5000aaaa00000001",
    }
    assert block.entries[1]["device_slot_number"] == 1
    assert block.entries[3]["device_slot_number"] == 2
    # The bay with no phys prints no slot number and no SAS address lines;
    # those fields must be absent rather than defaulted -- and must not be
    # filled from the trailing expander block's SAS address either.
    assert "device_slot_number" not in block.entries[2]
    assert "sas_address" not in block.entries[2]
    assert block.entries[2]["eiioe"] == 0


def test_aes_printed_index_is_global_not_per_type() -> None:
    """The bays-second fixture models what the KTN capture proves about real
    firmware: the printed ELEMENT INDEX continues across type descriptors
    (the bay block opens at 1, after the expander's 0). The parser must keep
    positions and printed indexes separate -- position identifies the
    element within its type; the printed value is a cross-check only."""
    text = (SYNTHETIC / "sg_aes_bays_second.txt").read_text()
    (bays,) = _bays(parse_additional_element_status(text))
    assert (bays.element_type, bays.type_index) == ("Array device slot", 1)
    assert [e["element_index"] for e in bays.entries] == [1, 2, 3, 4]
    assert [e["device_slot_number"] for e in bays.entries] == [1, 2, 3, 4]


def test_aes_eip0_descriptor_form_counts_positionally() -> None:
    """eip=0 descriptors print 'Element N descriptor' with no index field;
    they still occupy a position and their fields must attach to a fresh
    entry, not bleed into the previous element's."""
    text = (SYNTHETIC / "sg_aes_eip0.txt").read_text()
    (block,) = _bays(parse_additional_element_status(text))
    assert len(block.entries) == 3
    assert block.entries[0]["device_slot_number"] == 10
    assert "element_index" not in block.entries[1]
    assert block.entries[1]["device_slot_number"] == 11
    assert block.entries[1]["sas_address"] == "0x5000dddd00000001"
    assert block.entries[2] == {
        "element_index": 2,
        "eiioe": 0,
        "device_slot_number": 12,
        "sas_address": "0x5000dddd00000002",
    }


def test_aes_malformed_output_does_not_raise() -> None:
    assert parse_additional_element_status("total garbage\nnot ses output\n") == []
