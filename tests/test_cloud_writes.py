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


async def test_write_marks_pending_until_verify_completes(hass, controller):
    """Bug #11: a register must stay marked pending for the whole verify window, then clear."""
    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_register(43110, 33)

    assert controller.is_write_pending(43110) is True  # verify task not run yet (closed instead)

    controller.api.async_at_read = AsyncMock(return_value="33")
    with patch("custom_components.solisconnect.cloud.cloud_controller.asyncio.sleep", new=AsyncMock()):
        await controller._verify_write(CLOUD_CID_MAP[636], [33])

    assert controller.is_write_pending(43110) is False


async def test_tou_switch_write_marks_pending_until_verify_completes(hass, controller):
    controller.api.async_at_read_batch = AsyncMock(return_value={5916: "0", 5917: "0"})  # cold-cache bitfield read
    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_register(43707, 0b0101)

    assert controller.is_write_pending(43707) is True

    controller.api.async_at_read_batch = AsyncMock(return_value={5916: "1", 5918: "1"})
    with patch("custom_components.solisconnect.cloud.cloud_controller.asyncio.sleep", new=AsyncMock()):
        await controller._verify_tou_switch_write(0b0101)

    assert controller.is_write_pending(43707) is False


async def test_multi_register_write_without_mapping_raises(controller):
    """43143/43144 are now mapped (V1 TOU, issue #19); use a genuinely unmapped register pair."""
    with pytest.raises(HomeAssistantError, match="no control CID mapping"):
        await controller.async_write_holding_registers(43200, [10, 30])


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


def _seed_v1_tou_cache(hass, charge_current=50, discharge_current=40, hour=6, minute=0):
    from custom_components.solisconnect.sensor_data.cloud_mapping import V1_TOU_REGISTERS

    values = {43141: charge_current, 43142: discharge_current}
    for base in (43143, 43153, 43163):
        for offset in range(0, 8, 2):
            values[base + offset] = hour
            values[base + offset + 1] = minute
    for register in V1_TOU_REGISTERS:
        hass.data[DOMAIN][VALUES][f"{SERIAL}|{register}"] = values[register]


async def test_v1_tou_current_write_broadcasts_to_all_slots(hass, controller):
    """A single global-current write must compose a CID 103 value with that current applied
    uniformly across all 3 cloud slots (Modbus has no per-slot current), issue #19."""
    _seed_v1_tou_cache(hass)

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_register(43141, 100)

    args, _kwargs = controller.api.async_control.await_args
    assert args[1] == 103
    parts = args[2].split(",")
    assert len(parts) == 18
    assert parts[0] == parts[6] == parts[12] == "100"  # new charge current, all slots
    assert parts[1] == parts[7] == parts[13] == "40"  # discharge current unchanged
    assert parts[2] == "06:00"  # times preserved
    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43141"] == 100


async def test_v1_tou_time_field_write_preserves_other_registers(hass, controller):
    _seed_v1_tou_cache(hass)

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_registers(43143, [7, 30])  # slot 1 charge-start -> 07:30

    args, _kwargs = controller.api.async_control.await_args
    parts = args[2].split(",")
    assert parts[0] == "50" and parts[1] == "40"  # currents unchanged
    assert parts[2] == "07:30"  # slot 1 charge start updated
    assert parts[3] == "06:00"  # slot 1 charge end unchanged
    assert parts[8] == "06:00"  # slot 2 charge start unchanged


async def test_v1_tou_write_composes_from_cid_read_when_cache_incomplete(hass, controller):
    full_msg = "50,40,06:00,06:00,06:00,06:00,50,40,06:00,06:00,06:00,06:00,50,40,06:00,06:00,06:00,06:00"
    controller.api.async_at_read = AsyncMock(return_value=full_msg)

    with patch.object(controller.hass, "async_create_task", side_effect=_close_background_task):
        await controller.async_write_holding_register(43142, 45)  # discharge current only

    # async_at_read(cid=103) is called both to compose the current state and, separately,
    # to fetch the pre-write old_value for yuanzhi -- so it's called more than once here.
    controller.api.async_at_read.assert_any_await(SERIAL, 103)
    args, _kwargs = controller.api.async_control.await_args
    parts = args[2].split(",")
    assert parts[1] == parts[7] == parts[13] == "45"
    assert parts[0] == "50"


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


async def test_poll_does_not_overwrite_register_with_pending_write(hass, controller):
    """Bug #11: a poll landing inside the write-verify window must not stomp the optimistic value."""
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43110"] = 33  # optimistic write already applied
    controller._pending_write_registers.add(43110)
    controller.inverter_id = "id1"
    controller.api.async_inverter_detail = AsyncMock(return_value={})
    controller.api.async_at_read_batch = AsyncMock(return_value={636: "99"})  # stale cloud-side value

    with patch.object(hass, "async_create_task", side_effect=_close_background_task):
        retrieval = CloudDataRetrieval(hass=hass, controller=controller)
    await retrieval._update_cycle()

    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43110"] == 33


async def test_poll_does_not_overwrite_tou_switch_register_with_pending_write(hass, controller):
    hass.data[DOMAIN][VALUES][f"{SERIAL}|43707"] = 0b0101
    controller._pending_write_registers.add(43707)
    controller.inverter_id = "id1"
    controller.api.async_inverter_detail = AsyncMock(return_value={})
    controller.api.async_at_read_batch = AsyncMock(return_value={5916: "1", 5917: "1", 5918: "1"})

    with patch.object(hass, "async_create_task", side_effect=_close_background_task):
        retrieval = CloudDataRetrieval(hass=hass, controller=controller)
    await retrieval._update_cycle()

    assert hass.data[DOMAIN][VALUES][f"{SERIAL}|43707"] == 0b0101


def _mock_group(sensors):
    group = MagicMock()
    group.sensors = sensors
    return group


def _mock_sensor_entry(registrars, name="s", enabled=True):
    m = MagicMock()
    m.registrars = registrars
    m.name = name
    m.enabled = enabled
    return m


class TestV2EnablementGate:
    """CID 6798 TOU V2 support gate, issue #16."""

    async def test_disables_v2_sensors_when_cid_reports_unsupported(self, hass, controller):
        v2_sensor = _mock_sensor_entry([43708], name="v2_slot1_soc")
        other_sensor = _mock_sensor_entry([43110], name="storage_mode")
        controller._sensor_groups = [_mock_group([v2_sensor, other_sensor])]
        controller.api.async_at_read = AsyncMock(return_value="0")  # not the supported value

        with patch.object(hass, "async_create_task", side_effect=_close_background_task):
            retrieval = CloudDataRetrieval(hass=hass, controller=controller, disable_unmapped=True)
        with patch("custom_components.solisconnect.cloud.cloud_retrieval.mark_platform_entities_unavailable_for_base_sensors") as mock_mark:
            await retrieval._disable_v2_if_unsupported()

        controller.api.async_at_read.assert_awaited_once_with(SERIAL, 6798)
        assert v2_sensor.enabled is False
        assert other_sensor.enabled is True  # unrelated sensors are untouched
        mock_mark.assert_called_once_with(hass, [v2_sensor])

    async def test_leaves_v2_sensors_enabled_when_cid_reports_supported(self, hass, controller):
        v2_sensor = _mock_sensor_entry([43708], name="v2_slot1_soc")
        controller._sensor_groups = [_mock_group([v2_sensor])]
        controller.api.async_at_read = AsyncMock(return_value="43605")

        with patch.object(hass, "async_create_task", side_effect=_close_background_task):
            retrieval = CloudDataRetrieval(hass=hass, controller=controller, disable_unmapped=True)
        await retrieval._disable_v2_if_unsupported()

        assert v2_sensor.enabled is True

    async def test_gate_read_failure_leaves_sensors_unchanged(self, hass, controller):
        """A failed/undeterminable read must never disable sensors on a guess."""
        v2_sensor = _mock_sensor_entry([43708], name="v2_slot1_soc")
        controller._sensor_groups = [_mock_group([v2_sensor])]
        controller.api.async_at_read = AsyncMock(side_effect=SolisCloudApiError("boom"))

        with patch.object(hass, "async_create_task", side_effect=_close_background_task):
            retrieval = CloudDataRetrieval(hass=hass, controller=controller, disable_unmapped=True)
        await retrieval._disable_v2_if_unsupported()

        assert v2_sensor.enabled is True
