"""Stage 5: cloud write path — CID control calls, optimistic cache, verify/restore, routing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.solisconnect.cloud.api_client import SolisCloudApiError
from custom_components.solisconnect.cloud.cloud_controller import SolisCloudController
from custom_components.solisconnect.cloud.cloud_retrieval import CloudDataRetrieval
from custom_components.solisconnect.const import DOMAIN, VALUES
from custom_components.solisconnect.sensor_data.cloud_mapping import CLOUD_CID_MAP, REGISTER_TO_CID, cid_msg_from_words

SERIAL = "CLOUDSN1"


def _close_background_task(coro):
    coro.close()
    return MagicMock()


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
    assert cid_msg_from_words(CLOUD_CID_MAP[5948], [125]) == "12.5"


async def test_write_sends_control_with_old_value(hass, controller):
    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):  # verify task tested separately
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
    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
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


async def test_tou_time_pair_write_composes_full_slot_from_cache(hass, controller):
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43711"] = 9
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43712"] = 0
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43713"] = 10
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43714"] = 30
    controller.api.async_at_read = AsyncMock(return_value="09:00-10:30")

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_registers(43711, [8, 15])

    controller.api.async_control.assert_awaited_once_with(SERIAL, 5946, "08:15-10:30", old_value="09:00-10:30")
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43711"] == 8
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43712"] == 15
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43713"] == 10
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43714"] == 30


async def test_tou_time_pair_write_composes_full_slot_from_cid_read(hass, controller):
    controller.api.async_at_read = AsyncMock(return_value="09:00-10:30")

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_registers(43713, [11, 45])

    controller.api.async_control.assert_awaited_once_with(SERIAL, 5946, "09:00-11:45", old_value="09:00-10:30")


async def test_tou_switch_write_sends_changed_cid_with_old_bitfield(hass, controller):
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43707"] = 0b0011

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_register(43707, 0b0101)

    assert controller.api.async_control.await_args_list[0].args == (SERIAL, 5917, "0")
    assert controller.api.async_control.await_args_list[0].kwargs == {"old_value": "3"}
    assert controller.api.async_control.await_args_list[1].args == (SERIAL, 5918, "1")
    assert controller.api.async_control.await_args_list[1].kwargs == {"old_value": "1"}
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43707"] == 0b0101


async def test_tou_switch_write_reads_bitfield_when_cache_empty(hass, controller):
    controller.api.async_at_read_batch = AsyncMock(return_value={5916: "1", 5917: "0", 5918: "0"})

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_register(43707, 0b0011)

    controller.api.async_at_read_batch.assert_awaited_once()
    controller.api.async_control.assert_awaited_once_with(SERIAL, 5917, "1", old_value="1")


async def test_close_connection_cancels_pending_verify_tasks(hass, controller):
    """Verify tasks must not outlive the controller (no cache writes after unload)."""
    await controller.async_write_holding_register(43110, 33)  # schedules a real 15s-delayed verify task

    assert len(controller._verify_tasks) == 1
    task = next(iter(controller._verify_tasks))

    controller.close_connection()

    assert controller._verify_tasks == set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_verify_tou_switch_mismatch_restores_device_truth(hass, controller):
    controller.api.async_at_read_batch = AsyncMock(return_value={5916: "1", 5917: "0"})
    with patch("custom_components.solisconnect.cloud.cloud_controller.asyncio.sleep", new=AsyncMock()):
        await controller._verify_tou_switch_write(0b0011)

    assert controller.api.async_at_read_batch.await_count == 2
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43707"] == 0b0001


async def test_tou_switch_write_masks_stray_bits_in_cached_old_value(hass, controller):
    """A corrupted/stray high bit in the cached bitfield must not leak into the API's old_value."""
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43707"] = 0x1003  # bits 0-1 set, plus a stray bit 12

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_register(43707, 0b0101)

    assert controller.api.async_control.await_args_list[0].kwargs["old_value"] == "3"


def test_register_to_cid_excludes_tou_switch_register():
    """All 12 TOU V2 switch mappings share register 43707; it must not resolve to any single CID."""
    assert 43707 not in REGISTER_TO_CID


class TestMergeSwitchBits:
    """CloudDataRetrieval._merge_switch_bits: cold-cache-incomplete-batch handling for bug #10."""

    @pytest.fixture
    def retrieval(self, hass, controller):
        hass.data[DOMAIN][VALUES] = {}
        with patch.object(hass, "async_create_task", side_effect=_close_background_task):
            return CloudDataRetrieval(hass=hass, controller=controller)

    def test_cold_cache_incomplete_batch_defers_publish(self, retrieval):
        """Only 3 of 12 switch CIDs observed and no prior cache: must not guess the other 9 bits are off."""
        batch = {5916: "1", 5917: "0", 5927: "1"}
        assert retrieval._merge_switch_bits(batch) is None

    def test_cold_cache_complete_batch_publishes(self, retrieval):
        batch = {cid: ("1" if cid in (5916, 5927) else "0") for cid in [5916, 5917, 5918, 5919, 5920, 5921, 5922, 5923, 5924, 5925, 5926, 5927]}
        assert retrieval._merge_switch_bits(batch) == {43707: (1 << 0) | (1 << 11)}

    def test_warm_cache_partial_batch_only_touches_observed_bits(self, hass, retrieval):
        """With a warm cache, a CID missing from this poll's batch must leave its bit untouched."""
        hass.data[DOMAIN][VALUES][f"{SERIAL}|43707"] = 0b0110  # bits 1 and 2 set
        batch = {5916: "1"}  # only bit 0 observed this cycle
        assert retrieval._merge_switch_bits(batch) == {43707: 0b0111}
