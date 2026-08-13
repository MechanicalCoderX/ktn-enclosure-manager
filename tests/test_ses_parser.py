"""SES parser tests against captured KTN-STL3 output (spec §41, §42)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ktnmgr.enclosure.ses_parser import (
    build_telemetry,
    parse_configuration,
    parse_join,
    parse_overall_flags,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ktn-stl3"


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
    assert by_id[0].logical_id == "5006048004a54c3e"
    assert by_id[1].logical_id == "5006048004a4eabe"
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
    telemetry = build_telemetry("0x5006048004a54c3e", configuration_text, join_text)
    assert telemetry.enclosure_id == "0x5006048004a54c3e"
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
