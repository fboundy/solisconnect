from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solisconnect.const import CONTROLLER, REGISTER, SLAVE, VALUE
from custom_components.solisconnect.sensors.solis_binary_sensor import (
    SolisBinaryEntity,
    set_bit,
)


@pytest.fixture
def controller():
    mock = MagicMock()
    mock.connected.return_value = True
    mock.host = "inverter.local"
    mock.device_id = 1
    mock.identification = "test-id"
    mock.model = "S6"
    mock.device_identification = "XYZ"
    mock.sw_version = "1.0"
    mock.async_write_holding_register = AsyncMock()
    mock.async_read_holding_register = AsyncMock()
    return mock


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.create_task = MagicMock()
    return hass


@pytest.mark.asyncio
async def test_conflicts_self_use_mode(mock_hass, controller):
    entity_def = {
        "register": 43110,
        "bit_position": 0,
        "conflicts_with": (6, 11),
        "name": "Self-Use Mode",
    }

    initial = set_bit(set_bit(0, 6, True), 11, True)

    with (
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_get", return_value=initial),
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_save"),
    ):
        entity = SolisBinaryEntity(mock_hass, controller, entity_def)
        await entity.set_register_bit(True)

        expected = set_bit(set_bit(0, 6, False), 11, False)
        expected = set_bit(expected, 0, True)

        controller.async_write_holding_register.assert_awaited_once_with(43110, expected)


@pytest.mark.asyncio
async def test_requires_tou(mock_hass, controller):
    entity_def = {
        "register": 43110,
        "bit_position": 1,
        "requires": (0,),
        "name": "TOU (Self-Use)",
    }

    with (
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_get", return_value=0),
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_save"),
    ):
        entity = SolisBinaryEntity(mock_hass, controller, entity_def)
        await entity.set_register_bit(True)

        expected = set_bit(set_bit(0, 0, True), 1, True)
        controller.async_write_holding_register.assert_awaited_once_with(43110, expected)


@pytest.mark.asyncio
async def test_timed_charge_switch_clears_incompatible_modes(mock_hass, controller):
    """Turning on Mode - Timed Charge while in Peak Shaving must clear bit 11 and fall
    back to Self-Use, not write an invalid combo the firmware rejects (the bounce)."""
    from custom_components.solisconnect.data.solis_config import SOLIS_INVERTERS, InverterOptions
    from custom_components.solisconnect.sensor_data.switch_sensors import get_switch_sensors

    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(), "S2_WL_ST")
    entity_def = next(
        entity for group in get_switch_sensors(config) if group.get("register") == 43110 for entity in group["entities"] if entity["bit_position"] == 1
    )
    entity_def = {**entity_def, "register": 43110, "write_register": None}

    initial = set_bit(0, 11, True)  # Peak Shaving active, no Self-Use/Feed-In

    with (
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_get", return_value=initial),
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_save"),
    ):
        entity = SolisBinaryEntity(mock_hass, controller, entity_def)
        await entity.set_register_bit(True)

        expected = set_bit(set_bit(0, 0, True), 1, True)  # Peak Shaving cleared, Self-Use + TOU set
        controller.async_write_holding_register.assert_awaited_once_with(43110, expected)


@pytest.mark.asyncio
async def test_conflicts_and_requires_combined(mock_hass, controller):
    entity_def = {
        "register": 43110,
        "bit_position": 4,
        "conflicts_with": (0, 6),
        "requires": (1,),
        "name": "Reserve Battery Mode",
    }

    initial = set_bit(set_bit(set_bit(0, 0, True), 6, True), 1, True)

    with (
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_get", return_value=initial),
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_save"),
    ):
        entity = SolisBinaryEntity(mock_hass, controller, entity_def)
        await entity.set_register_bit(True)

        expected = set_bit(set_bit(0, 1, True), 4, True)
        controller.async_write_holding_register.assert_awaited_once_with(43110, expected)


@pytest.mark.asyncio
async def test_cold_cache_reads_live_value_and_preserves_bits(mock_hass, controller):
    """Issue #402: toggling a bit before the register has been polled must not
    start the read-modify-write from 0 (which clears every other bit). The live
    register value must be read from the inverter first."""
    entity_def = {
        "register": 43110,
        "bit_position": 1,
        "name": "TOU (Self-Use)",
    }

    live_value = set_bit(set_bit(0, 0, True), 4, True)  # bits 0 and 4 already set on the device
    controller.async_read_holding_register.return_value = [live_value]

    with (
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_get", return_value=None),
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_save"),
    ):
        entity = SolisBinaryEntity(mock_hass, controller, entity_def)
        await entity.set_register_bit(True)

        controller.async_read_holding_register.assert_awaited_once_with(43110, 1)
        expected = set_bit(live_value, 1, True)  # other bits preserved
        controller.async_write_holding_register.assert_awaited_once_with(43110, expected)


@pytest.mark.asyncio
async def test_cold_cache_failed_live_read_skips_write(mock_hass, controller):
    """Issue #402: if the cache is empty and the live read fails, do not write
    anything rather than risk clearing the other bits in the register."""
    entity_def = {
        "register": 43110,
        "bit_position": 1,
        "name": "TOU (Self-Use)",
    }

    controller.async_read_holding_register.return_value = None

    with (
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_get", return_value=None),
        patch("custom_components.solisconnect.sensors.solis_binary_sensor.cache_save"),
    ):
        entity = SolisBinaryEntity(mock_hass, controller, entity_def)
        await entity.set_register_bit(True)

        controller.async_read_holding_register.assert_awaited_once_with(43110, 1)
        controller.async_write_holding_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_modbus_update_pushes_state(mock_hass, controller):
    """Bug #13: a register update must push entity state immediately, not just rely on polling."""
    entity_def = {
        "register": 43110,
        "bit_position": 1,
        "name": "TOU (Self-Use)",
    }
    entity = SolisBinaryEntity(mock_hass, controller, entity_def)
    entity.schedule_update_ha_state = MagicMock()

    data = {REGISTER: 43110, VALUE: set_bit(0, 1, True), CONTROLLER: str(controller.host), SLAVE: controller.device_id}
    entity.handle_modbus_update(data)

    entity.schedule_update_ha_state.assert_called_once()
    assert entity.is_on is True


@pytest.mark.asyncio
async def test_handle_modbus_update_pushes_state_for_connection_switch(mock_hass, controller):
    """Register 5 (connection enable switch) must also push state on update."""
    entity_def = {"register": 5, "name": "Connection Enabled"}
    controller.enabled = True
    entity = SolisBinaryEntity(mock_hass, controller, entity_def)
    entity.schedule_update_ha_state = MagicMock()

    data = {REGISTER: 5, VALUE: 1, CONTROLLER: str(controller.host), SLAVE: controller.device_id}
    entity.handle_modbus_update(data)

    entity.schedule_update_ha_state.assert_called_once()
    assert entity.is_on is True
