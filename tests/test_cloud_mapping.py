"""Contract tests: every cloud mapping must round-trip through the REAL master definitions.

For each CloudFieldMapping we locate the matching entity in hybrid_sensors.py, build a real
SolisBaseSensor from it, and assert convert(encode(cloud_value)) == cloud_value (within the
register's granularity). This pins the mapping multipliers/data types to the master dicts —
if either side drifts, these tests fail.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.solis_modbus.data.enums import DataType
from custom_components.solis_modbus.sensor_data.cloud_mapping import (
    CLOUD_CID_MAP,
    CLOUD_INPUT_MAP,
    CloudFieldMapping,
    encode_cid_value,
    encode_engineering_value,
    registers_covered,
    unit_factor,
)
from custom_components.solis_modbus.sensor_data.hybrid_sensors import hybrid_sensors
from custom_components.solis_modbus.sensors.solis_base_sensor import SolisBaseSensor


def _master_entities() -> dict[tuple[int, ...], dict]:
    """Index hybrid_sensors entity definitions by their register tuple."""
    index = {}
    for group in hybrid_sensors:
        for ent in group.get("entities", []):
            if ent.get("name", "reserve") == "reserve":
                continue
            regs = tuple(int(r) for r in ent["register"])
            index[regs] = ent
    return index


MASTER = _master_entities()


def _mock_controller():
    controller = MagicMock()
    controller.inverter_config.model = "S6-EH1P6K-L"
    controller.inverter_config.features = []
    controller.inverter_config.wattage_chosen = 6000
    return controller


def _base_sensor_for(entity_def: dict) -> SolisBaseSensor:
    return SolisBaseSensor(
        hass=MagicMock(),
        controller=_mock_controller(),
        unique_id="test",
        name=entity_def.get("name"),
        registrars=[int(r) for r in entity_def["register"]],
        write_register=None,
        multiplier=entity_def.get("multiplier", 1),
        data_type=entity_def.get("data_type", None),
    )


def _simple_mappings() -> list[CloudFieldMapping]:
    return [m for m in CLOUD_INPUT_MAP.values() if m.encoder is None]


def test_every_mapping_has_a_master_definition():
    for mapping in _simple_mappings():
        assert mapping.registers in MASTER, f"{mapping.api_field}: registers {mapping.registers} not found in hybrid_sensors"


def test_mapping_multipliers_match_master():
    for mapping in _simple_mappings():
        master_mult = MASTER[mapping.registers].get("multiplier", 1)
        # 0 and 1 are both "raw" in _convert_raw_value; otherwise must be identical
        if master_mult in (0, 1):
            assert mapping.multiplier in (0, 1), f"{mapping.api_field}: master is raw, mapping scales by {mapping.multiplier}"
        else:
            assert mapping.multiplier == master_mult, f"{mapping.api_field}: mapping multiplier {mapping.multiplier} != master {master_mult}"


def test_mapping_s16_flags_match_master():
    for mapping in _simple_mappings():
        master_type = MASTER[mapping.registers].get("data_type", None)
        if master_type == DataType.S16:
            assert mapping.data_type == DataType.S16, f"{mapping.api_field}: master is S16, mapping is {mapping.data_type}"


@pytest.mark.parametrize(
    ("api_field", "cloud_value", "cloud_unit"),
    [
        ("eToday", 22.8, None),
        ("eTotal", 8.46, "MWh"),
        ("eTotal", 8460, "kWh"),
        ("eMonth", 31.2, "kWh"),
        ("uPv1", 97.3, None),
        ("iPv1", 0.2, None),
        ("uA", 246.1, None),
        ("pac", 0.027, "kW"),
        ("pac", 27, "W"),
        ("pac", -2.5, "kW"),  # importing
        ("apparentPower", 1.43, "kVA"),
        ("inverterTemperature", 45.4, None),
        ("inverterTemperature", -10.5, None),  # S16 negative
        ("fac", 50.12, None),
        ("currentState", 3, None),
        ("batteryVoltage", 52.4, None),
        ("storageBatteryCurrent", -30.4, None),  # S16 negative (charging)
        ("batteryCapacitySoc", 59.0, None),
        ("familyLoadPower", 0.915, "kW"),
        ("psum", 0.531, "kW"),
        ("psum", -1.2, "kW"),  # S32 negative
        ("batteryTodayChargeEnergy", 17.0, "kWh"),
        ("batteryTotalChargeEnergy", 7.68, "MWh"),
        ("gridPurchasedTotalEnergy", 41.634, "GWh"),
        ("gridSellTodayEnergy", 13.05, "kWh"),
        ("homeLoadTodayEnergy", 17.0, "kWh"),
        ("uAc1", 246.4, None),
        ("iAc1", 6.1, None),
    ],
)
def test_round_trip_through_master_definition(api_field, cloud_value, cloud_unit):
    mapping = CLOUD_INPUT_MAP[api_field]
    words = encode_engineering_value(mapping, cloud_value, cloud_unit)
    assert words is not None, f"{api_field}: {cloud_value} {cloud_unit} not encodable"

    sensor = _base_sensor_for(MASTER[mapping.registers])
    decoded = sensor.convert_value([words[reg] for reg in mapping.registers])

    # Expected value in the register's engineering unit
    expected = float(cloud_value) * unit_factor(cloud_unit, mapping.unit)
    granularity = mapping.multiplier if mapping.multiplier not in (0, 1) else 1
    assert decoded == pytest.approx(expected, abs=granularity / 2 + 1e-9), f"{api_field}: {decoded} != {expected} (granularity {granularity})"


def test_battery_power_encoder_charging():
    mapping = CLOUD_INPUT_MAP["batteryPower"]
    words = mapping.encoder(-1.596, {"batteryPowerStr": "kW"})
    assert words[33135] == 0  # charging
    assert (words[33149] << 16 | words[33150]) == 1596  # magnitude in W


def test_battery_power_encoder_discharging():
    mapping = CLOUD_INPUT_MAP["batteryPower"]
    words = mapping.encoder(2.0, {"batteryPowerStr": "kW"})
    assert words[33135] == 1  # discharging
    assert (words[33149] << 16 | words[33150]) == 2000


def test_negative_value_for_unsigned_register_is_dropped():
    mapping = CLOUD_INPUT_MAP["batteryCapacitySoc"]  # U16
    assert encode_engineering_value(mapping, -5, None) is None


def test_non_numeric_value_is_dropped():
    mapping = CLOUD_INPUT_MAP["eToday"]
    assert encode_engineering_value(mapping, "n/a", None) is None


def test_unknown_unit_falls_back_to_unscaled():
    assert unit_factor("furlongs", "kWh") == 1.0
    assert unit_factor(None, "kWh") == 1.0
    assert unit_factor("MWh", None) == 1.0
    assert unit_factor("MWh", "kWh") == 1000.0


def test_cid_values_encode_to_register_words():
    # Live values observed: cid 636 msg "33" -> register 43110 = 33; cid 157 "52" -> 43024 = 52
    assert encode_cid_value(CLOUD_CID_MAP[636], "33") == {43110: 33}
    assert encode_cid_value(CLOUD_CID_MAP[157], "52") == {43024: 52}
    assert encode_cid_value(CLOUD_CID_MAP[158], "15") == {43011: 15}
    assert encode_cid_value(CLOUD_CID_MAP[160], "not_a_number") is None


def test_registers_covered_includes_inputs_and_cids():
    covered = registers_covered()
    assert 33035 in covered  # eToday
    assert 33135 in covered  # battery direction (custom encoder)
    assert 43110 in covered  # cid 636
    assert 33022 not in covered  # RTC deliberately unmapped
