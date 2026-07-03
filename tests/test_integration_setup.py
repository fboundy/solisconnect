import asyncio
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solisconnect.const import CONTROLLER, DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.mark.asyncio
async def test_setup_entry(hass: HomeAssistant):
    """Test setting up the integration."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SN123456",  # Match the serial to skip deferred migration logic
        data={
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "inverter_serial": "SN123456",
            "model": "S6-EH1P",
            "poll_interval_fast": 10,
            "poll_interval_normal": 15,
            "poll_interval_slow": 30,
        },
        title="Solis Inverter",
    )
    config_entry.add_to_hass(hass)

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connected", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            return_value=[1, 2, 3],
        ),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_holding_register",
            return_value=[1, 2, 3],
        ),
    ):
        # Setup the config entry
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        # Verify Config Entry is Loaded
        assert config_entry.state.value == "loaded"

        # Verify controller is stored using Entry ID
        assert DOMAIN in hass.data
        assert CONTROLLER in hass.data[DOMAIN]
        assert config_entry.entry_id in hass.data[DOMAIN][CONTROLLER]
        assert hass.data[DOMAIN][CONTROLLER][config_entry.entry_id] is not None

        # Test unload
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()

        # Verify controller is removed from hass.data
        assert config_entry.entry_id not in hass.data[DOMAIN][CONTROLLER]


@pytest.mark.asyncio
async def test_setup_entry_staggers_on_shared_link(hass: HomeAssistant):
    """Second config entry on the same host:port should be staggered; the first should not."""
    entry_1 = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SN111111",
        data={
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "inverter_serial": "SN111111",
            "model": "S6-EH1P",
            "poll_interval_fast": 10,
            "poll_interval_normal": 15,
            "poll_interval_slow": 30,
        },
        title="Solis Inverter 1",
    )
    entry_2 = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SN222222",
        data={
            "host": "1.2.3.4",
            "port": 502,
            "slave": 2,
            "inverter_serial": "SN222222",
            "model": "S6-EH1P",
            "poll_interval_fast": 10,
            "poll_interval_normal": 15,
            "poll_interval_slow": 30,
        },
        title="Solis Inverter 2",
    )
    # entry_2 is added to hass only after entry_1's setup completes: HA auto-schedules
    # setup for every pending config entry of a domain the first time that domain's
    # component loads, so adding both entries up front would set up entry_2 concurrently
    # in the background during entry_1's own setup call.
    entry_1.add_to_hass(hass)

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connected", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            return_value=[1, 2, 3],
        ),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_holding_register",
            return_value=[1, 2, 3],
        ),
        patch("custom_components.solisconnect.asyncio.sleep", wraps=asyncio.sleep) as mock_sleep,
    ):
        # Background polling loops also call asyncio.sleep, so assert on the specific
        # stagger delay (1.5s for one existing same-link controller) rather than call counts.
        assert await hass.config_entries.async_setup(entry_1.entry_id)
        await hass.async_block_till_done()
        assert not any(call.args and call.args[0] == pytest.approx(1.5) for call in mock_sleep.call_args_list)

        mock_sleep.reset_mock()
        entry_2.add_to_hass(hass)

        assert await hass.config_entries.async_setup(entry_2.entry_id)
        await hass.async_block_till_done()
        assert any(call.args and call.args[0] == pytest.approx(1.5) for call in mock_sleep.call_args_list)


@pytest.mark.asyncio
async def test_setup_entry_missing_serial_raises_error(hass: HomeAssistant):
    """Test setup failure when serial is missing (ConfigEntryError)."""
    # Create an entry WITHOUT a serial number
    # We set version=3 to SKIP migration and force it to hit async_setup_entry
    config_entry = MockConfigEntry(domain=DOMAIN, data={"host": "1.2.3.4", "port": 502, "slave": 1, "model": "S6-EH1P"}, version=3)
    config_entry.add_to_hass(hass)

    # Now this will run async_setup_entry, fail the validation, and raise ConfigEntryError
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # ConfigEntryError results in 'setup_error'
    assert config_entry.state.value == "setup_error"
