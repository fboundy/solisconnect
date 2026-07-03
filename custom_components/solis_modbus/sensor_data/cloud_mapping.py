"""SolisCloud inverterDetail/CID <-> Modbus register mapping.

The register cache stores RAW register words (what Modbus would return), so entities and
derived sensors work identically regardless of which protocol produced the data. This module
inverse-converts cloud engineering values into raw words:

    raw = round(cloud_value * unit_factor / multiplier)

Each mapping's `multiplier` MUST equal the multiplier of the matching entity definition in
hybrid_sensors.py — tests/test_cloud_mapping.py enforces that contract by round-tripping
every mapping through a real SolisBaseSensor built from the master definitions.

Unit strings: the cloud scales values per magnitude (the same field can arrive as kWh, MWh
or even GWh) and reports the unit in a companion "<field>Str" key; `unit` here is the fixed
engineering unit of the Modbus register the value is written into.

Deliberately NOT mapped: the RTC registers (33022-33027). Cloud data lags minutes behind
real time, which would trip the clock-drift auto-correction and write a wrong time to the
inverter.

Sources: the SolisConnect prototype's dual-transport table, mkuthan/solis-cloud-control and
hultenvp/solis-sensor, cross-checked against a live inverterDetail dump (783 fields) and
live atRead sysCommand.registerAddr metadata (636->43110, 157->43024, 158->43011).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from custom_components.solis_modbus.data.enums import DataType

_LOGGER = logging.getLogger(__name__)

# Factors to a family's base unit (W, Wh, VA, var). Voltage/current/frequency/percent
# arrive unscaled.
UNIT_FACTORS = {
    "W": 1.0,
    "kW": 1e3,
    "MW": 1e6,
    "GW": 1e9,
    "Wh": 1.0,
    "kWh": 1e3,
    "MWh": 1e6,
    "GWh": 1e9,
    "VA": 1.0,
    "kVA": 1e3,
    "MVA": 1e6,
    "var": 1.0,
    "Var": 1.0,
    "kvar": 1e3,
    "kVar": 1e3,
}

_WARNED_UNITS: set[str] = set()


def unit_factor(from_unit: str | None, to_unit: str | None) -> float:
    """Factor converting a value in from_unit into to_unit; 1.0 when either is unknown."""
    if not from_unit or not to_unit or from_unit == to_unit:
        return 1.0
    try:
        return UNIT_FACTORS[from_unit] / UNIT_FACTORS[to_unit]
    except KeyError:
        if from_unit not in _WARNED_UNITS:
            _WARNED_UNITS.add(from_unit)
            _LOGGER.warning("Unknown SolisCloud unit %r (target %r); using value unscaled", from_unit, to_unit)
        return 1.0


@dataclass(frozen=True)
class CloudFieldMapping:
    """inverterDetail JSON field -> input register(s)."""

    registers: tuple[int, ...]  # hi word first for 32-bit values
    api_field: str
    unit: str | None = None  # engineering unit of the Modbus register value
    api_unit_field: str | None = None  # defaults to f"{api_field}Str" when unit is set
    multiplier: float = 1.0  # MUST match the master entity definition (0/1 mean raw)
    data_type: DataType = DataType.U16
    # Custom encoder: (cloud_value, full_detail_dict) -> {register: word}; overrides the rest
    encoder: Callable[[float, dict], dict[int, int]] | None = field(default=None, compare=False)

    @property
    def unit_field(self) -> str | None:
        if self.api_unit_field:
            return self.api_unit_field
        return f"{self.api_field}Str" if self.unit else None


@dataclass(frozen=True)
class CloudCidMapping:
    """Control CID <-> holding register(s). The atRead 'msg' is the register's raw value
    scaled by `multiplier` (identically to the entity definition)."""

    registers: tuple[int, ...]
    cid: int
    multiplier: float = 1.0
    data_type: DataType = DataType.U16


def _words_for_raw(raw: int, registers: tuple[int, ...], data_type: DataType) -> dict[int, int] | None:
    """Split a raw engineering-integer into register words (two's complement for negatives)."""
    if len(registers) == 1:
        if raw < 0:
            if data_type != DataType.S16:
                return None  # negative value for an unsigned register: drop rather than corrupt
            raw += 0x10000
        if not 0 <= raw <= 0xFFFF:
            return None
        return {registers[0]: raw}
    if len(registers) == 2:
        if not -(2**31) <= raw < 2**32:
            return None
        raw &= 0xFFFFFFFF
        return {registers[0]: (raw >> 16) & 0xFFFF, registers[1]: raw & 0xFFFF}
    return None


def encode_engineering_value(mapping: CloudFieldMapping, value: float, cloud_unit: str | None = None) -> dict[int, int] | None:
    """Convert a cloud engineering value into raw register words; None if unrepresentable."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    value *= unit_factor(cloud_unit, mapping.unit)
    raw = round(value) if mapping.multiplier in (0, 1) else round(value / mapping.multiplier)
    return _words_for_raw(raw, mapping.registers, mapping.data_type)


def encode_cid_value(mapping: CloudCidMapping, msg: str) -> dict[int, int] | None:
    """Convert an atRead 'msg' value into raw holding-register words."""
    try:
        value = float(msg)
    except (TypeError, ValueError):
        return None
    raw = round(value) if mapping.multiplier in (0, 1) else round(value / mapping.multiplier)
    return _words_for_raw(raw, mapping.registers, mapping.data_type)


def _encode_battery_power(value: float, detail: dict) -> dict[int, int]:
    """Signed cloud batteryPower (negative = charging) -> magnitude registers + direction flag.

    Modbus stores the battery power magnitude in 33149/33150 and the direction in 33135
    (0 = charging, 1 = discharging); the derived sensor applies the sign.
    """
    watts = round(float(value) * unit_factor(detail.get("batteryPowerStr"), "W"))
    direction = 0 if watts < 0 else 1
    magnitude = abs(watts)
    words = _words_for_raw(magnitude, (33149, 33150), DataType.U32) or {}
    words[33135] = direction
    return words


# --- inverterDetail field -> input registers -------------------------------------------------
# fmt: off
CLOUD_INPUT_MAP: dict[str, CloudFieldMapping] = {m.api_field: m for m in [
    # Energy generation
    CloudFieldMapping(registers=(33029, 33030), api_field="eTotal", unit="kWh", multiplier=0, data_type=DataType.U32),
    CloudFieldMapping(registers=(33031, 33032), api_field="eMonth", unit="kWh", multiplier=1, data_type=DataType.U32),
    CloudFieldMapping(registers=(33035,), api_field="eToday", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33037, 33038), api_field="eYear", unit="kWh", multiplier=1, data_type=DataType.U32),
    # PV strings
    CloudFieldMapping(registers=(33049,), api_field="uPv1", multiplier=0.1),
    CloudFieldMapping(registers=(33050,), api_field="iPv1", multiplier=0.1),
    CloudFieldMapping(registers=(33051,), api_field="uPv2", multiplier=0.1),
    CloudFieldMapping(registers=(33052,), api_field="iPv2", multiplier=0.1),
    CloudFieldMapping(registers=(33053,), api_field="uPv3", multiplier=0.1),
    CloudFieldMapping(registers=(33054,), api_field="iPv3", multiplier=0.1),
    CloudFieldMapping(registers=(33055,), api_field="uPv4", multiplier=0.1),
    CloudFieldMapping(registers=(33056,), api_field="iPv4", multiplier=0.1),
    # Grid AC
    CloudFieldMapping(registers=(33073,), api_field="uA", multiplier=0.1),
    CloudFieldMapping(registers=(33074,), api_field="uB", multiplier=0.1),
    CloudFieldMapping(registers=(33075,), api_field="uC", multiplier=0.1),
    CloudFieldMapping(registers=(33076,), api_field="iA", multiplier=0.1),
    CloudFieldMapping(registers=(33077,), api_field="iB", multiplier=0.1),
    CloudFieldMapping(registers=(33078,), api_field="iC", multiplier=0.1),
    CloudFieldMapping(registers=(33079, 33080), api_field="pac", unit="W", multiplier=1, data_type=DataType.S32),
    CloudFieldMapping(registers=(33081, 33082), api_field="reactivePower", unit="var", api_unit_field="reactivePowerStr", multiplier=0, data_type=DataType.S32),
    CloudFieldMapping(registers=(33083, 33084), api_field="apparentPower", unit="VA", multiplier=0, data_type=DataType.S32),
    # Inverter
    CloudFieldMapping(registers=(33093,), api_field="inverterTemperature", multiplier=0.1, data_type=DataType.S16),
    CloudFieldMapping(registers=(33094,), api_field="fac", multiplier=0.01),
    CloudFieldMapping(registers=(33095,), api_field="currentState", multiplier=0),
    # Battery
    CloudFieldMapping(registers=(33133,), api_field="batteryVoltage", multiplier=0.1),
    CloudFieldMapping(registers=(33134,), api_field="storageBatteryCurrent", multiplier=0.1, data_type=DataType.S16),
    CloudFieldMapping(registers=(33139,), api_field="batteryCapacitySoc", multiplier=0),
    CloudFieldMapping(registers=(33140,), api_field="batteryHealthSoh", multiplier=0),
    CloudFieldMapping(registers=(33149, 33150, 33135), api_field="batteryPower", unit="W", encoder=_encode_battery_power),
    # Loads
    CloudFieldMapping(registers=(33147,), api_field="familyLoadPower", unit="W", multiplier=0),
    CloudFieldMapping(registers=(33148,), api_field="bypassLoadPower", unit="W", multiplier=0),
    CloudFieldMapping(registers=(33151, 33152), api_field="psum", unit="W", multiplier=0, data_type=DataType.S32),
    # Battery energy counters
    CloudFieldMapping(registers=(33161, 33162), api_field="batteryTotalChargeEnergy", unit="kWh", multiplier=1, data_type=DataType.U32),
    CloudFieldMapping(registers=(33163,), api_field="batteryTodayChargeEnergy", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33164,), api_field="batteryYesterdayChargeEnergy", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33165, 33166), api_field="batteryTotalDischargeEnergy", unit="kWh", multiplier=1, data_type=DataType.U32),
    CloudFieldMapping(registers=(33167,), api_field="batteryTodayDischargeEnergy", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33168,), api_field="batteryYesterdayDischargeEnergy", unit="kWh", multiplier=0.1),
    # Grid energy counters
    CloudFieldMapping(registers=(33169, 33170), api_field="gridPurchasedTotalEnergy", unit="kWh", multiplier=0, data_type=DataType.U32),
    CloudFieldMapping(registers=(33171,), api_field="gridPurchasedTodayEnergy", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33172,), api_field="gridPurchasedYesterdayEnergy", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33173, 33174), api_field="gridSellTotalEnergy", unit="kWh", multiplier=0, data_type=DataType.U32),
    CloudFieldMapping(registers=(33175,), api_field="gridSellTodayEnergy", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33176,), api_field="gridSellYesterdayEnergy", unit="kWh", multiplier=0.1),
    # Household energy counters
    CloudFieldMapping(registers=(33177, 33178), api_field="homeLoadTotalEnergy", unit="kWh", multiplier=0, data_type=DataType.U32),
    CloudFieldMapping(registers=(33179,), api_field="homeLoadTodayEnergy", unit="kWh", multiplier=0.1),
    CloudFieldMapping(registers=(33180,), api_field="homeLoadYesterdayEnergy", unit="kWh", multiplier=0.1),
    # Meter
    CloudFieldMapping(registers=(33251,), api_field="uAc1", multiplier=0.1),
    CloudFieldMapping(registers=(33252,), api_field="iAc1", multiplier=0.01),
    CloudFieldMapping(registers=(33253,), api_field="uAc2", multiplier=0.1),
    CloudFieldMapping(registers=(33254,), api_field="iAc2", multiplier=0.01),
    CloudFieldMapping(registers=(33255,), api_field="uAc3", multiplier=0.1),
    CloudFieldMapping(registers=(33256,), api_field="iAc3", multiplier=0.01),
]}
# fmt: on

# --- control CID <-> holding registers -------------------------------------------------------
CLOUD_CID_MAP: dict[int, CloudCidMapping] = {
    m.cid: m
    for m in [
        CloudCidMapping(registers=(43110,), cid=636),  # storage mode bitfield (== Modbus 43110)
        CloudCidMapping(registers=(43024,), cid=157),  # reserved / backup SOC %
        CloudCidMapping(registers=(43011,), cid=158),  # over-discharge SOC %
        CloudCidMapping(registers=(43018,), cid=160),  # force-charge SOC %
        CloudCidMapping(registers=(43074,), cid=696, multiplier=100),  # feed-in power limit (raw = W/100, cid msg = W)
    ]
}

# Write routing: which CID a holding-register write lands on
REGISTER_TO_CID: dict[int, CloudCidMapping] = {register: mapping for mapping in CLOUD_CID_MAP.values() for register in mapping.registers}


def cid_msg_from_words(mapping: CloudCidMapping, words: list[int]) -> str:
    """Convert raw register word(s) into the control-API value string (inverse of encode_cid_value)."""
    if len(words) == 1:
        raw = words[0]
        if mapping.data_type == DataType.S16 and raw > 0x7FFF:
            raw -= 0x10000
    else:
        combined = (words[0] << 16) | (words[1] & 0xFFFF)
        raw = combined - 0x100000000 if combined & 0x80000000 else combined
    value = raw if mapping.multiplier in (0, 1) else raw * mapping.multiplier
    return str(round(value))


def registers_covered() -> set[int]:
    """All registers the cloud protocol can populate (for availability decisions)."""
    covered: set[int] = set()
    for mapping in CLOUD_INPUT_MAP.values():
        covered.update(mapping.registers)
    for cid_mapping in CLOUD_CID_MAP.values():
        covered.update(cid_mapping.registers)
    return covered
