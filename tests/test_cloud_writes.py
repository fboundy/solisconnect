"""Stage 5: cloud write path — CID control calls, optimistic cache, verify/restore, routing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.solisconnect.cloud.api_client import SolisCloudApiError
from custom_components.solisconnect.cloud.cloud_controller import SolisCloudController
from custom_components.solisconnect.const import DOMAIN, VALUES
from custom_components.solisconnect.sensor_data.cloud_mapping import CLOUD_CID_MAP, cid_msg_from_words

SERIAL = "CLOUDSN1"


@pytest.fixture
def controller(hass):
    hass.data.setdefault(DOMAIN, {}).setdefault(VALUES, {})
    inverter_config = MagicMock()
    inverter_config.model = "S6-EH1P"
    api = MagicMock()
    api.async_at_read = AsyncMock(return_value="35")
    api.async_control = AsyncMock(return_value=True)
    return SolisCloudController(
        hass=hass,
        inverter_config=inverter_config,
        api=api,
        plant_id="plant1",
        serial_number=SERIAL,
        inverter_id="123",
    )


def test_cid_msg_from_words_applies_multiplier():
    assert cid_msg_from_words(CLOUD_CID_MAP[636], [33]) == "33"
    assert cid_msg_from_words(CLOUD_CID_MAP[157], [52]) == "52"
    assert cid_msg_from_words(CLOUD_CID_MAP[696], [50]) == "5000"  # raw W/100 -> msg W


async def test_write_sends_control_with_old_value(hass, controller):
    with patch.object(controller.hass, "async_create_task"):  # verify task tested separately
        await controller.async_write_holding_register(43110, 33)

    controller.api.async_at_read.assert_awaited_once_with(SERIAL, 636)
    controller.api.async_control.assert_awaited_once_with(SERIAL, 636, "33", old_value="35")
    # Optimistic cache update visible immediately
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43110"] == 33


async def test_write_unmapped_register_raises(controller):
    with pytest.raises(HomeAssistantError, match="no control CID mapping"):
        await controller.async_write_holding_register(43000, 26)
    controller.api.async_control.assert_not_awaited()


async def test_write_failure_raises_home_assistant_error(hass, controller):
    controller.api.async_control = AsyncMock(side_effect=SolisCloudApiError("boom"))
    with pytest.raises(HomeAssistantError, match="failed"):
        await controller.async_write_holding_register(43110, 33)
    # No optimistic update on failure
    assert f"{SERIAL}|43110" not in hass.data[DOMAIN][VALUES]


async def test_write_proceeds_without_old_value(hass, controller):
    controller.api.async_at_read = AsyncMock(side_effect=SolisCloudApiError("read failed"))
    with patch.object(controller.hass, "async_create_task"):
        await controller.async_write_holding_register(43024, 55)
    controller.api.async_control.assert_awaited_once_with(SERIAL, 157, "55", old_value=None)


async def test_verify_confirms_write(hass, controller):
    controller.api.async_at_read = AsyncMock(return_value="33")
    with patch("custom_components.solisconnect.cloud.cloud_controller.asyncio.sleep", new=AsyncMock()):
        await controller._verify_write(CLOUD_CID_MAP[636], [33])
    # Verified on first read: single verify read
    assert controller.api.async_at_read.await_count == 1


async def test_verify_mismatch_restores_device_truth(hass, controller):
    """If the device reports a different value after the write, the cache must show the device's value."""
    controller.api.async_at_read = AsyncMock(return_value="35")  # device kept old value
    with patch("custom_components.solisconnect.cloud.cloud_controller.asyncio.sleep", new=AsyncMock()):
        await controller._verify_write(CLOUD_CID_MAP[636], [33])

    assert controller.api.async_at_read.await_count == 2  # retried once
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43110"] == 35  # device truth restored


async def test_multi_register_write_without_mapping_raises(controller):
    with pytest.raises(HomeAssistantError, match="no control CID mapping"):
        await controller.async_write_holding_registers(43143, [10, 30])
