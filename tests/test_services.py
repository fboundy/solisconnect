from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.solisconnect.const import CONTROLLER, DOMAIN
from custom_components.solisconnect.helpers import get_controller


@pytest.fixture
def mock_controller():
    controller = MagicMock()
    controller.host = "1.2.3.4"
    controller.device_id = 1
    controller.modbus = None  # simulates a cloud-only/bare controller; hubs set this to their sub-controller
    controller.async_write_holding_register = AsyncMock(return_value=None)
    return controller


@pytest.mark.asyncio
async def test_service_write_holding_register(hass: HomeAssistant, mock_controller):
    """Test solis_write_holding_register service."""
    from custom_components.solisconnect.const import CONTROLLER

    # Store controller
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][CONTROLLER] = {"1.2.3.4_1": mock_controller}

    with patch("custom_components.solisconnect.get_controller", return_value=mock_controller):
        # Register the services (requires setting up the integration or manually registering)
        from custom_components.solisconnect import async_setup

        await async_setup(hass, {})

        # Call service
        await hass.services.async_call(DOMAIN, "solis_write_holding_register", {"address": 123, "value": 456, "host": "1.2.3.4"}, blocking=True)

        mock_controller.async_write_holding_register.assert_called_with(123, 456)


@pytest.mark.asyncio
async def test_service_write_holding_register_no_host(hass: HomeAssistant, mock_controller):
    """Test solis_write_holding_register service without host (broadcast to all)."""
    from custom_components.solisconnect.const import CONTROLLER

    # Store controller in hass.data
    hass.data[DOMAIN] = {CONTROLLER: {"1.2.3.4_1": mock_controller}}

    from custom_components.solisconnect import async_setup

    await async_setup(hass, {})

    await hass.services.async_call(DOMAIN, "solis_write_holding_register", {"address": 123, "value": 456}, blocking=True)

    mock_controller.async_write_holding_register.assert_called_with(123, 456)


@pytest.mark.asyncio
async def test_service_write_holding_register_no_target_with_multiple_controllers_raises(hass: HomeAssistant, mock_controller):
    """Bug #12: silently broadcasting a write to every configured inverter is dangerous; require a target."""
    other_controller = MagicMock()
    other_controller.host = "5.6.7.8"
    other_controller.device_id = 1
    other_controller.async_write_holding_register = AsyncMock(return_value=None)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][CONTROLLER] = {"1.2.3.4_1": mock_controller, "5.6.7.8_1": other_controller}

    from custom_components.solisconnect import async_setup

    await async_setup(hass, {})

    with pytest.raises(ServiceValidationError, match="Multiple"):
        await hass.services.async_call(DOMAIN, "solis_write_holding_register", {"address": 123, "value": 456}, blocking=True)

    mock_controller.async_write_holding_register.assert_not_called()
    other_controller.async_write_holding_register.assert_not_called()


@pytest.mark.asyncio
async def test_service_write_holding_register_unmatched_target_raises(hass: HomeAssistant, mock_controller):
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][CONTROLLER] = {"1.2.3.4_1": mock_controller}

    from custom_components.solisconnect import async_setup

    await async_setup(hass, {})

    with pytest.raises(ServiceValidationError, match="No configured"):
        await hass.services.async_call(DOMAIN, "solis_write_holding_register", {"address": 123, "value": 456, "host": "9.9.9.9"}, blocking=True)

    mock_controller.async_write_holding_register.assert_not_called()


@pytest.mark.asyncio
async def test_service_write_holding_register_matches_by_serial(hass: HomeAssistant, mock_controller):
    """Serial matching must work even while the hub's own `host` property is protocol-dependent."""
    mock_controller.host = "soliscloud"  # simulates a dual-protocol hub currently active on cloud
    mock_controller.serial_number = "SN12345"

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][CONTROLLER] = {"1.2.3.4_1": mock_controller}

    from custom_components.solisconnect import async_setup

    await async_setup(hass, {})

    await hass.services.async_call(DOMAIN, "solis_write_holding_register", {"address": 123, "value": 456, "serial": "SN12345"}, blocking=True)

    mock_controller.async_write_holding_register.assert_called_with(123, 456)


@pytest.mark.asyncio
async def test_service_write_holding_register_write_failure_raises(hass: HomeAssistant, mock_controller):
    """Bug #12: a write failure must surface as a service error, not vanish into a fire-and-forget task."""
    mock_controller.async_write_holding_register = AsyncMock(side_effect=RuntimeError("boom"))

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][CONTROLLER] = {"1.2.3.4_1": mock_controller}

    from custom_components.solisconnect import async_setup

    await async_setup(hass, {})

    with pytest.raises(HomeAssistantError, match="boom"):
        await hass.services.async_call(DOMAIN, "solis_write_holding_register", {"address": 123, "value": 456, "host": "1.2.3.4"}, blocking=True)


def test_get_controller_matches_host_via_modbus_sub_controller_when_hub_is_cloud_active():
    """Bug #12: a hub's `.host` delegates to the active protocol (e.g. "soliscloud"); host matching
    must read the hub's `.modbus` sub-controller instead, so it still works after failover."""
    hub = MagicMock()
    hub.host = "soliscloud"  # currently active protocol is cloud
    hub.device_id = 1  # cloud controller's hardcoded device_id, not the real Modbus slave
    hub.modbus.host = "1.2.3.4"  # the hub's real, protocol-independent Modbus link
    hub.modbus.device_id = 3

    hass = MagicMock()
    hass.data = {DOMAIN: {CONTROLLER: {"entry1": hub}}}

    assert get_controller(hass, host="1.2.3.4", slave=3) is hub
    assert get_controller(hass, host="soliscloud", slave=1) is None  # the active-protocol host must not match


def test_get_controller_matches_serial_regardless_of_host():
    hub = MagicMock()
    hub.serial_number = "SN999"
    hub.host = "soliscloud"

    hass = MagicMock()
    hass.data = {DOMAIN: {CONTROLLER: {"entry1": hub}}}

    assert get_controller(hass, serial="SN999") is hub
    assert get_controller(hass, serial="SNOTHER") is None


@pytest.mark.asyncio
async def test_service_set_time(hass: HomeAssistant):
    """Test solis_write_time service."""
    from datetime import time

    from custom_components.solisconnect.const import TIME_ENTITIES

    mock_entity = MagicMock()
    mock_entity.entity_id = "time.test_time"
    mock_entity.async_set_value = MagicMock(return_value=None)

    # async_set_value must be awaitable
    async def async_set_value(val):
        pass

    mock_entity.async_set_value = MagicMock(side_effect=async_set_value)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][TIME_ENTITIES] = [mock_entity]

    from custom_components.solisconnect import async_setup

    await async_setup(hass, {})

    await hass.services.async_call(DOMAIN, "solis_write_time", {"entity_id": "time.test_time", "time": "12:30:00"}, blocking=True)

    # Check if called with correct time object
    mock_entity.async_set_value.assert_called()
    call_args = mock_entity.async_set_value.call_args
    assert call_args[0][0] == time(12, 30, 0)
