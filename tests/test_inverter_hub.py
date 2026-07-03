"""Stage 6: dual-protocol orchestration — manual switching, failover, write routing."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.solisconnect.const import (
    PROTO_BOTH_FAILOVER,
    PROTO_BOTH_MANUAL,
    PROTOCOL_CLOUD,
    PROTOCOL_MODBUS,
)
from custom_components.solisconnect.inverter_hub import (
    MIN_DWELL_SECONDS,
    PRIMARY_RECOVERY_SECONDS,
    SolisInverterHub,
)

SERIAL = "DUALSN1"


def _hub(hass, mode, failover_primary=None) -> SolisInverterHub:
    inverter_config = MagicMock()
    inverter_config.model = "S6-EH1P"
    hub = SolisInverterHub(
        hass=hass,
        entry_id="entry1",
        inverter_config=inverter_config,
        serial_number=SERIAL,
        mode=mode,
        failover_primary=failover_primary,
    )
    hub.modbus = MagicMock()
    hub.modbus.serial_number = SERIAL
    hub.modbus.async_write_holding_register = AsyncMock()
    hub.modbus.connect = AsyncMock(return_value=True)
    hub.modbus.async_read_input_register = AsyncMock(return_value=[100])
    hub.modbus.last_modbus_success = datetime.now(UTC)
    hub.cloud = MagicMock()
    hub.cloud.serial_number = SERIAL
    hub.cloud.async_write_holding_register = AsyncMock()
    hub.cloud.connected = MagicMock(return_value=True)
    hub.cloud.api.async_at_read = AsyncMock(return_value="33")
    return hub


def _patch_retrievals():
    return (
        patch("custom_components.solisconnect.data_retrieval.DataRetrieval"),
        patch("custom_components.solisconnect.cloud.cloud_retrieval.CloudDataRetrieval"),
    )


async def test_manual_switch_stops_one_retrieval_starts_other(hass):
    hub = _hub(hass, PROTO_BOTH_MANUAL)
    modbus_patch, cloud_patch = _patch_retrievals()
    with modbus_patch as modbus_retrieval, cloud_patch as cloud_retrieval:
        modbus_retrieval.return_value.async_stop = AsyncMock()
        cloud_retrieval.return_value.async_stop = AsyncMock()

        await hub.async_start()
        assert hub.active_protocol == PROTOCOL_MODBUS
        assert modbus_retrieval.call_count == 1
        assert cloud_retrieval.call_count == 0

        await hub.async_set_active(PROTOCOL_CLOUD, reason="test")
        assert hub.active_protocol == PROTOCOL_CLOUD
        assert hub.active is hub.cloud
        modbus_retrieval.return_value.async_stop.assert_awaited_once()  # old poller stopped
        assert cloud_retrieval.call_count == 1  # new poller started

        # Switching to the already-active protocol is a no-op
        await hub.async_set_active(PROTOCOL_CLOUD, reason="again")
        assert cloud_retrieval.call_count == 1

        await hub.async_stop()


async def test_failover_switches_when_primary_stale(hass):
    hub = _hub(hass, PROTO_BOTH_FAILOVER, failover_primary=PROTOCOL_MODBUS)
    modbus_patch, cloud_patch = _patch_retrievals()
    with modbus_patch as modbus_retrieval, cloud_patch as cloud_retrieval:
        modbus_retrieval.return_value.async_stop = AsyncMock()
        cloud_retrieval.return_value.async_stop = AsyncMock()
        await hub.async_start()

        # Healthy primary: no switch
        await hub._health_tick()
        assert hub.active_protocol == PROTOCOL_MODBUS

        # Stale Modbus + dwell elapsed -> failover to cloud
        hub.modbus.last_modbus_success = datetime.now(UTC) - timedelta(seconds=600)
        hub._last_switch = datetime.now(UTC) - timedelta(seconds=MIN_DWELL_SECONDS + 1)
        await hub._health_tick()
        assert hub.active_protocol == PROTOCOL_CLOUD

        await hub.async_stop()


async def test_failover_respects_min_dwell(hass):
    hub = _hub(hass, PROTO_BOTH_FAILOVER, failover_primary=PROTOCOL_MODBUS)
    modbus_patch, cloud_patch = _patch_retrievals()
    with modbus_patch as modbus_retrieval, cloud_patch:
        modbus_retrieval.return_value.async_stop = AsyncMock()
        await hub.async_start()

        hub.modbus.last_modbus_success = datetime.now(UTC) - timedelta(seconds=600)
        hub._last_switch = datetime.now(UTC)  # just switched
        await hub._health_tick()
        assert hub.active_protocol == PROTOCOL_MODBUS  # dwell not elapsed

        await hub.async_stop()


async def test_failover_auto_return_needs_sustained_health(hass):
    hub = _hub(hass, PROTO_BOTH_FAILOVER, failover_primary=PROTOCOL_MODBUS)
    modbus_patch, cloud_patch = _patch_retrievals()
    with modbus_patch as modbus_retrieval, cloud_patch as cloud_retrieval:
        modbus_retrieval.return_value.async_stop = AsyncMock()
        cloud_retrieval.return_value.async_stop = AsyncMock()
        await hub.async_start()
        hub._active_name = PROTOCOL_CLOUD  # running on secondary
        hub._start_retrieval(PROTOCOL_CLOUD)
        hub._last_switch = datetime.now(UTC) - timedelta(seconds=MIN_DWELL_SECONDS + 1)

        # First healthy probe starts the recovery clock but must not switch yet
        await hub._health_tick()
        assert hub.active_protocol == PROTOCOL_CLOUD
        assert hub._primary_healthy_since is not None

        # Not yet healthy long enough
        hub._primary_healthy_since = datetime.now(UTC) - timedelta(seconds=PRIMARY_RECOVERY_SECONDS - 30)
        await hub._health_tick()
        assert hub.active_protocol == PROTOCOL_CLOUD

        # Healthy long enough -> auto-return
        hub._primary_healthy_since = datetime.now(UTC) - timedelta(seconds=PRIMARY_RECOVERY_SECONDS + 1)
        await hub._health_tick()
        assert hub.active_protocol == PROTOCOL_MODBUS

        await hub.async_stop()


async def test_failover_probe_failure_resets_recovery_clock(hass):
    hub = _hub(hass, PROTO_BOTH_FAILOVER, failover_primary=PROTOCOL_MODBUS)
    modbus_patch, cloud_patch = _patch_retrievals()
    with modbus_patch as modbus_retrieval, cloud_patch as cloud_retrieval:
        modbus_retrieval.return_value.async_stop = AsyncMock()
        cloud_retrieval.return_value.async_stop = AsyncMock()
        await hub.async_start()
        hub._active_name = PROTOCOL_CLOUD
        hub._primary_healthy_since = datetime.now(UTC) - timedelta(seconds=PRIMARY_RECOVERY_SECONDS + 100)

        hub.modbus.async_read_input_register = AsyncMock(return_value=None)  # probe fails
        await hub._health_tick()
        assert hub._primary_healthy_since is None
        assert hub.active_protocol == PROTOCOL_CLOUD

        await hub.async_stop()


async def test_write_routes_to_active_with_fallback(hass):
    hub = _hub(hass, PROTO_BOTH_MANUAL)
    hub._active_name = PROTOCOL_CLOUD

    # Cloud cannot write this register; healthy Modbus takes it with a warning
    hub.cloud.async_write_holding_register = AsyncMock(side_effect=HomeAssistantError("no mapping"))
    hub.modbus.connected = MagicMock(return_value=True)
    await hub.async_write_holding_register(43000, 26)
    hub.modbus.async_write_holding_register.assert_awaited_once_with(43000, 26)


async def test_concurrent_switches_are_serialized(hass):
    """Two overlapping async_set_active calls must not stop/start retrievals twice."""
    hub = _hub(hass, PROTO_BOTH_MANUAL)
    modbus_patch, cloud_patch = _patch_retrievals()
    with modbus_patch as modbus_retrieval, cloud_patch as cloud_retrieval:

        async def slow_stop():
            await asyncio.sleep(0.05)

        modbus_retrieval.return_value.async_stop = AsyncMock(side_effect=slow_stop)
        cloud_retrieval.return_value.async_stop = AsyncMock()
        await hub.async_start()

        await asyncio.gather(
            hub.async_set_active(PROTOCOL_CLOUD, reason="select entity"),
            hub.async_set_active(PROTOCOL_CLOUD, reason="health tick"),
        )

        # The second call became a no-op inside the lock: one stop, one new retrieval
        modbus_retrieval.return_value.async_stop.assert_awaited_once()
        assert cloud_retrieval.call_count == 1
        assert hub.active_protocol == PROTOCOL_CLOUD

        await hub.async_stop()


async def test_write_falls_back_from_modbus_to_cloud(hass):
    """A failing Modbus write raises HomeAssistantError, which routes the write to healthy cloud."""
    hub = _hub(hass, PROTO_BOTH_FAILOVER)
    hub._active_name = PROTOCOL_MODBUS

    hub.modbus.async_write_holding_register = AsyncMock(side_effect=HomeAssistantError("write failed"))
    await hub.async_write_holding_register(43110, 33)
    hub.cloud.async_write_holding_register.assert_awaited_once_with(43110, 33)


async def test_write_fallback_requires_healthy_other(hass):
    hub = _hub(hass, PROTO_BOTH_MANUAL)
    hub._active_name = PROTOCOL_CLOUD
    hub.cloud.async_write_holding_register = AsyncMock(side_effect=HomeAssistantError("no mapping"))
    hub.modbus.connected = MagicMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="no mapping"):
        await hub.async_write_holding_register(43000, 26)
    hub.modbus.async_write_holding_register.assert_not_awaited()


def test_replace_sensor_group_applies_to_both_controllers(hass):
    """Bug #13: a recovery edit applied only to the active controller is lost across a switch."""
    hub = _hub(hass, PROTO_BOTH_MANUAL)
    hub.modbus.replace_sensor_group = MagicMock()
    hub.cloud.replace_sensor_group = MagicMock()

    old_group, new_groups = object(), [object(), object()]
    hub.replace_sensor_group(old_group, new_groups)

    hub.modbus.replace_sensor_group.assert_called_once_with(old_group, new_groups)
    hub.cloud.replace_sensor_group.assert_called_once_with(old_group, new_groups)


def test_enable_disable_connection_applies_to_both_controllers(hass):
    """Bug #13: toggling the connection switch must not leave the standby protocol untouched."""
    hub = _hub(hass, PROTO_BOTH_MANUAL)

    hub.enable_connection()
    hub.modbus.enable_connection.assert_called_once()
    hub.cloud.enable_connection.assert_called_once()

    hub.disable_connection()
    hub.modbus.disable_connection.assert_called_once()
    hub.cloud.disable_connection.assert_called_once()


async def test_health_tick_skips_when_previous_probe_still_running(hass):
    """Bug #13: overlapping timer fires must not race the in-flight probe's state bookkeeping."""
    hub = _hub(hass, PROTO_BOTH_FAILOVER, failover_primary=PROTOCOL_MODBUS)
    modbus_patch, cloud_patch = _patch_retrievals()
    with modbus_patch as modbus_retrieval, cloud_patch:
        modbus_retrieval.return_value.async_stop = AsyncMock()
        await hub.async_start()
        hub._health_tick_running = True
        try:
            hub.modbus.last_modbus_success = datetime.now(UTC) - timedelta(seconds=600)
            hub._last_switch = datetime.now(UTC) - timedelta(seconds=MIN_DWELL_SECONDS + 1)
            await hub._health_tick()
            # The tick returned immediately without evaluating failover at all
            assert hub.active_protocol == PROTOCOL_MODBUS
        finally:
            hub._health_tick_running = False

        await hub.async_stop()
