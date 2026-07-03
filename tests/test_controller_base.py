"""Tests for the protocol-neutral controller surface (SolisControllerBase) and cache scoping."""

from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.solisconnect.const import (
    CONN_TYPE_TCP,
    CONTROLLER,
    DOMAIN,
    REGISTER,
    SCOPE,
    SENSOR_ENTITIES,
    SLAVE,
    VALUE,
    VALUES,
)
from custom_components.solisconnect.controller_base import SolisControllerBase
from custom_components.solisconnect.helpers import (
    controller_scope,
    is_correct_controller,
    register_cache_key,
    register_update_signal,
)
from custom_components.solisconnect.modbus_controller import ModbusController


def _make_modbus_controller(serial_number=None, host="192.168.1.100", port=502, device_id=1):
    inverter_config = MagicMock()
    inverter_config.model = "Test Model"
    with patch("custom_components.solisconnect.modbus_controller.ModbusClientManager"):
        return ModbusController(
            hass=MagicMock(),
            connection_type=CONN_TYPE_TCP,
            host=host,
            port=port,
            inverter_config=inverter_config,
            device_id=device_id,
            serial_number=serial_number,
        )


def test_modbus_controller_is_a_solis_controller():
    controller = _make_modbus_controller(serial_number="SN123")
    assert isinstance(controller, SolisControllerBase)


def test_cache_scope_prefers_serial_number():
    controller = _make_modbus_controller(serial_number="SN123")
    assert controller.cache_scope == "SN123"
    assert register_cache_key(controller, 33000) == "SN123|33000"
    assert register_update_signal(controller, 33000) == f"{DOMAIN}_SN123_33000"


def test_cache_scope_falls_back_to_connection_identity():
    controller = _make_modbus_controller(serial_number=None)
    assert controller.cache_scope == "192.168.1.100:502|1"
    assert register_cache_key(controller, 33000) == "192.168.1.100:502|1|33000"


def test_same_serial_gives_same_scope_across_transports():
    """Two controllers for the same inverter must land values on identical cache keys."""
    modbus_a = _make_modbus_controller(serial_number="SN123", host="10.0.0.1")
    modbus_b = _make_modbus_controller(serial_number="SN123", host="10.0.0.2", port=1502, device_id=2)
    assert register_cache_key(modbus_a, 33057) == register_cache_key(modbus_b, 33057)
    assert register_update_signal(modbus_a, 33057) == register_update_signal(modbus_b, 33057)


def test_parallel_inverters_on_one_link_stay_distinct():
    c1 = _make_modbus_controller(serial_number="SN_A", host="10.0.0.1", device_id=1)
    c2 = _make_modbus_controller(serial_number="SN_B", host="10.0.0.1", device_id=2)
    assert register_cache_key(c1, 33139) != register_cache_key(c2, 33139)


def test_is_correct_controller_scope_payload():
    controller = _make_modbus_controller(serial_number="SN123")
    assert is_correct_controller(controller, {SCOPE: "SN123", REGISTER: 1, VALUE: 0})
    assert not is_correct_controller(controller, {SCOPE: "OTHER", REGISTER: 1, VALUE: 0})


def test_is_correct_controller_legacy_payload_fallback():
    """Payloads without a scope fall back to host+slave comparison."""
    controller = _make_modbus_controller(serial_number="SN123")
    assert is_correct_controller(controller, {CONTROLLER: "192.168.1.100", SLAVE: 1})
    assert not is_correct_controller(controller, {CONTROLLER: "9.9.9.9", SLAVE: 1})


def test_controller_scope_handles_bare_mocks():
    """Mocks without a usable cache_scope keep the historical connection scope."""
    mock = MagicMock()
    mock.connection_id = "10.0.0.1:502"
    mock.device_id = 2
    assert controller_scope(mock) == "10.0.0.1:502|2"


async def test_second_platform_setup_does_not_wipe_cache(hass: HomeAssistant):
    """Regression: a second entry's sensor platform setup must not reset the shared cache."""
    from custom_components.solisconnect.sensor import async_setup_entry

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][VALUES] = {"SN123|33000": 42}
    existing_entity = MagicMock()
    hass.data[DOMAIN][SENSOR_ENTITIES] = [existing_entity]

    controller = MagicMock()
    controller.sensor_groups = []
    controller.derived_sensors = []

    with patch("custom_components.solisconnect.sensor.get_controller_from_entry", return_value=controller):
        await async_setup_entry(hass, MagicMock(), MagicMock())

    assert hass.data[DOMAIN][VALUES] == {"SN123|33000": 42}
    assert existing_entity in hass.data[DOMAIN][SENSOR_ENTITIES]
