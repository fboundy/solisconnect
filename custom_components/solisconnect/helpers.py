import logging
import struct
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_utils

from custom_components.solisconnect import DOMAIN
from custom_components.solisconnect.const import (
    CONTROLLER,
    DRIFT_COUNTER,
    NUMBER_ENTITIES,
    REGISTER,
    SCOPE,
    SENSOR_DERIVED_ENTITIES,
    SENSOR_ENTITIES,
    SLAVE,
    SWITCH_ENTITIES,
    TIME_ENTITIES,
    VALUE,
    VALUES,
)

_LOGGER = logging.getLogger(__name__)


def hex_to_ascii(hex_value):
    # Convert hexadecimal to decimal
    decimal_value = hex_value

    # Split into bytes
    byte1 = (decimal_value >> 8) & 0xFF
    byte2 = decimal_value & 0xFF

    # Convert bytes to ASCII characters
    ascii_chars = "".join([chr(byte) for byte in [byte1, byte2]])

    return ascii_chars


def unique_id_generator(controller, third_value, fourth_value=None):
    # new method to generate unique id
    if fourth_value is None:
        if controller.device_serial_number is not None:
            return f"{DOMAIN}_{controller.device_serial_number}_{third_value}"

        if controller.identification is not None:
            return f"{DOMAIN}_{controller.identification}_{third_value}"

        return f"{DOMAIN}_{controller.host}_{third_value}"
    else:
        if controller.device_serial_number is not None:
            return f"{DOMAIN}_{controller.device_serial_number}_{third_value}_{fourth_value}"

        if controller.identification is not None:
            return f"{DOMAIN}_{controller.identification}_{third_value}_{fourth_value}"

        return f"{DOMAIN}_{controller.host}_{third_value}_{fourth_value}"


def unique_id_generator_binary(controller, register, bit_position, on_value):
    if controller.device_serial_number is not None:
        return f"{DOMAIN}_{controller.device_serial_number}_{register}_{on_value if on_value is not None else bit_position}"
    if controller.identification is not None:
        return f"{DOMAIN}_{controller.identification}_{register}_{on_value if on_value is not None else bit_position}"

    return f"{DOMAIN}_{controller.host}_{register}_{on_value if on_value is not None else bit_position}"


def extract_serial_number(values):
    packed = struct.pack(">" + "H" * len(values), *values)
    return packed.decode("ascii", errors="ignore").strip("\x00\r\n ")


def clock_drift_test(hass, controller, year, month, day, hours, minutes, seconds):
    current_time = dt_utils.now()
    try:
        # RTC year register holds 0-99 (offset from 2000)
        device_time = datetime(2000 + year, month, day, hours, minutes, seconds, tzinfo=current_time.tzinfo)
        total_drift = abs((current_time - device_time).total_seconds())
    except ValueError:
        # RTC holds an impossible date (e.g. zeros after backup-power loss) — force a correction
        total_drift = float("inf")

    # Ensure structure
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    drift_counter = hass.data[DOMAIN].get(DRIFT_COUNTER, 0)
    clock_adjusted = False

    if total_drift > 60:
        if drift_counter > 5:
            if controller.connected():
                hass.create_task(
                    controller.async_write_holding_registers(
                        43000,
                        [
                            current_time.year % 100,
                            current_time.month,
                            current_time.day,
                            current_time.hour,
                            current_time.minute,
                            current_time.second,
                        ],
                    )
                )
                clock_adjusted = True
        else:
            hass.data[DOMAIN][DRIFT_COUNTER] = drift_counter + 1
    else:
        hass.data[DOMAIN][DRIFT_COUNTER] = 0

    _LOGGER.debug(f"Drift: {total_drift}s, Counter: {drift_counter}, Adjusted: {clock_adjusted}")
    return clock_adjusted


def decode_inverter_model(hex_value):
    """
    Decodes an inverter model code into its protocol version and description.

    :param hex_value: The hexadecimal or decimal inverter model code.
    :return: A tuple (protocol_version, model_description)
    """
    # Convert hexadecimal string to integer if necessary
    if isinstance(hex_value, str):
        hex_value = int(hex_value, 16)

    # Extract high byte (protocol version) and low byte (inverter model)
    protocol_version = (hex_value >> 8) & 0xFF
    inverter_model = hex_value & 0xFF

    inverter_models = {
        0x00: "No definition",
        0x10: "1-Phase Grid-Tied Inverter (0.7-8K1P / 7-10K1P)",
        0x20: "3-Phase Grid-Tied Inverter (3-20K 3P)",
        0x21: "3-Phase Grid-Tied Inverter (25-50K / 50-70K / 80-110K / 90-136K / 125K / 250K)",
        0x30: "1-Phase LV Hybrid Inverter",
        0x31: "1-Phase LV AC Coupled Energy Storage Inverter",
        0x32: "5-15kWh All-in-One Hybrid",
        0x40: "1-Phase HV Hybrid Inverter",
        0x50: "3-Phase LV Hybrid Inverter",
        0x60: "3-Phase HV Hybrid Inverter (5G)",
        0x70: "S6 3-Phase HV Hybrid (5-10kW)",
        0x71: "S6 3-Phase HV Hybrid (12-20kW)",
        0x72: "S6 3-Phase LV Hybrid (10-15kW)",
        0x73: "S6 3-Phase HV Hybrid (50kW)",
        0x80: "1-Phase HV Hybrid Inverter (S6)",
        0x90: "1-Phase LV Hybrid Inverter (S6)",
        0x91: "S6 1-Phase LV AC Coupled Hybrid",
        0xA0: "OGI Off-Grid Inverter",
        0xA1: "S6 1-Phase LV Off-Grid Hybrid",
    }

    # Get model description or default to "Unknown Model"
    model_description = inverter_models.get(inverter_model, "Unknown Model")

    return protocol_version, model_description


# HMI firmware version at or above this value supports TOU V2 (Grid Time of Use). Verified
# live: an inverter reporting hmiVersionAll/register 33002 = "4B00" is confirmed running V2,
# so 0x4B00 is the boundary-inclusive minimum, not an earlier (wrong) 0xFB00 guess.
V2_HMI_VERSION_THRESHOLD = 0x4B00


def hmi_version_supports_v2(raw: int | str | None) -> bool | None:
    """Whether an HMI firmware version indicates TOU V2 support.

    Accepts either the raw Modbus register integer (register 33002) or the SolisCloud
    hex string (`inverterDetail` field `hmiVersionAll`, e.g. "4B00") — both encode the
    same raw value. Returns None if undeterminable rather than raising, so callers can
    fall back to whatever the user already configured.
    """
    if raw is None:
        return None
    try:
        value = int(raw, 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return None
    return value >= V2_HMI_VERSION_THRESHOLD


def controller_scope(controller) -> str:
    """Protocol-neutral scope shared by all controllers talking to one inverter.

    Prefers the controller's `cache_scope` (serial-number based, see SolisControllerBase);
    falls back to the historical connection_id|slave scope for controllers without one
    (e.g. bare mocks in tests).
    """
    scope = getattr(controller, "cache_scope", None)
    if isinstance(scope, str) and scope:
        return scope
    conn = getattr(controller, "connection_id", None)
    if not isinstance(conn, str):
        conn = str(getattr(controller, "host", "") or "")
    try:
        slave = int(getattr(controller, "device_id", 1))
    except (TypeError, ValueError):
        slave = 1
    return f"{conn}|{slave}"


def register_cache_key(controller, register: str | int) -> str:
    """Build a register cache key scoped per inverter (serial-based when available)."""
    return f"{controller_scope(controller)}|{register}"


def cache_save(hass: HomeAssistant, controller, register: str | int, value):
    hass.data[DOMAIN][VALUES][register_cache_key(controller, register)] = value


def cache_get(hass: HomeAssistant, controller, register: str | int):
    return hass.data[DOMAIN][VALUES].get(register_cache_key(controller, register), None)


def mark_platform_entities_unavailable_for_base_sensors(hass: HomeAssistant, disabled_sensors: list) -> None:
    """Mark sensor/number platform entities unavailable when their SolisBaseSensor is dynamically disabled."""
    if not disabled_sensors:
        return
    disabled_set = frozenset(disabled_sensors)
    domain_data = hass.data.get(DOMAIN) or {}
    for bucket in (SENSOR_ENTITIES, NUMBER_ENTITIES):
        for ent in domain_data.get(bucket) or []:
            base = getattr(ent, "base_sensor", None)
            if base is not None and base in disabled_set:
                ent._attr_available = False
                ent.schedule_update_ha_state()


_SENSITIVE_CONF_KEYS = ("password", "key_secret", "key_id", "username", "token")


def redact_config(config: dict) -> dict:
    """Copy of a config dict safe for logging (cloud credentials masked)."""
    return {k: ("***" if any(s in k for s in _SENSITIVE_CONF_KEYS) else v) for k, v in config.items()}


def remove_entities_for_controller(hass: HomeAssistant, controller) -> None:
    """Drop one controller's entities from the shared per-domain entity lists on unload.

    The lists are shared across config entries (platform setups extend them), so unload
    must filter out this entry's entities or reloads would accumulate stale objects.
    """

    def _entity_controller(entity):
        base = getattr(entity, "base_sensor", None)
        if base is not None:
            return base.controller
        return getattr(entity, "_modbus_controller", None)

    domain_data = hass.data.get(DOMAIN) or {}
    for bucket in (SENSOR_ENTITIES, SENSOR_DERIVED_ENTITIES, NUMBER_ENTITIES, SWITCH_ENTITIES, TIME_ENTITIES):
        entities = domain_data.get(bucket)
        if entities:
            domain_data[bucket] = [e for e in entities if _entity_controller(e) is not controller]


def set_controller(hass: HomeAssistant, controller, config_entry: ConfigEntry):
    """Store controller in hass.data using the Config Entry ID."""
    # We use entry_id because it is unique, immutable, and works for both TCP and Serial.
    hass.data[DOMAIN][CONTROLLER][config_entry.entry_id] = controller


def get_controller_from_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Get controller from config entry using the Config Entry ID."""
    return hass.data[DOMAIN][CONTROLLER].get(config_entry.entry_id)


def get_controller(hass: HomeAssistant, host: str | None = None, slave: int = 1, serial: str | None = None):
    """
    Find a stored controller (a SolisInverterHub, per config entry) by inverter serial or
    by its configured Modbus host/slave. Used by services (write_holding_register) that
    only know a target host or serial.

    Serial is protocol-stable and preferred: a hub's `host`/`device_id` properties delegate
    to whichever protocol is currently active (e.g. the literal string "soliscloud" while
    cloud is active), so host/slave matching instead reads the hub's `.modbus` sub-controller
    directly, which always reflects its real configured Modbus link regardless of which
    protocol is currently active.
    """
    for controller in hass.data[DOMAIN][CONTROLLER].values():
        if serial is not None:
            if getattr(controller, "serial_number", None) == serial:
                return controller
            continue
        modbus = getattr(controller, "modbus", None)
        if modbus is not None:
            configured_host = getattr(modbus, "host", None)
            configured_slave = getattr(modbus, "device_id", None)
        else:
            configured_host = getattr(controller, "host", None)
            configured_slave = getattr(controller, "device_id", None)
        if configured_host == host and configured_slave == slave:
            return controller
    return None


def split_s32(s32_values: list[int]):
    high_word = s32_values[0] - (1 << 16) if s32_values[0] & (1 << 15) else s32_values[0]
    low_word = s32_values[1] - (1 << 16) if s32_values[1] & (1 << 15) else s32_values[1]

    # Combine the high and low words to form a 32-bit signed/unsigned integer
    return (high_word << 16) | (low_word & 0xFFFF)


def _any_in(target: list[int], collection: set[int]) -> bool:
    return any(item in collection for item in target)


def is_correct_controller(controller, data: dict) -> bool:
    """Check whether a register-update payload belongs to this controller's inverter."""
    scope = data.get(SCOPE)
    if isinstance(scope, str):
        return scope == controller_scope(controller)
    # Legacy payloads without a scope: fall back to host + slave comparison.
    return str(getattr(controller, "host", None)) == str(data.get(CONTROLLER)) and int(getattr(controller, "device_id", 1)) == int(data.get(SLAVE, 1))


def register_update_signal(controller, register: int) -> str:
    """Dispatcher signal for one register on a specific inverter (not recorded by the HA recorder)."""
    return f"{DOMAIN}_{controller_scope(controller)}_{int(register)}"


def notify_register_update(hass: HomeAssistant, controller, register: int, value) -> None:
    """Notify listeners for a single register; replaces bus.async_fire(DOMAIN, ...) for register data."""
    payload = {
        REGISTER: register,
        VALUE: value,
        SCOPE: controller_scope(controller),
        CONTROLLER: getattr(controller, "host", None),
        SLAVE: int(getattr(controller, "device_id", 1)),
    }
    async_dispatcher_send(hass, register_update_signal(controller, register), payload)
