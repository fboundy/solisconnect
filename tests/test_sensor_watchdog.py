"""Watchdog staleness timeout: poll speeds are seconds, the grace period is minutes."""

from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.solisconnect.data.enums import PollSpeed
from custom_components.solisconnect.sensors.solis_sensor import SolisSensor


def _entity(poll_speed=PollSpeed.NORMAL, interval_s=30) -> SolisSensor:
    base = MagicMock()
    base.name = "Test Sensor"
    base.unique_id = "uid1"
    base.registrars = [33000]
    base.device_class = None
    base.unit_of_measurement = None
    base.state_class = None
    base.hidden = False
    base.enabled = True
    base.multiplier = 1
    base.poll_speed = poll_speed
    base.controller.poll_speed = {poll_speed: interval_s}
    return SolisSensor(MagicMock(), base)


def test_watchdog_timeout_treats_poll_speed_as_seconds():
    # Cloud-style 300s poll interval: 5 min + 10 min grace, not 310 minutes
    entity = _entity(interval_s=300)
    assert entity._update_timeout == timedelta(seconds=300) + timedelta(minutes=10)


def test_watchdog_timeout_fast_modbus_poll():
    entity = _entity(poll_speed=PollSpeed.FAST, interval_s=5)
    assert entity._update_timeout == timedelta(seconds=5) + timedelta(minutes=10)


async def test_watchdog_marks_sensor_unavailable_after_timeout():
    entity = _entity(interval_s=5)
    entity.schedule_update_ha_state = MagicMock()
    entity._attr_available = True
    entity._last_update = entity._last_update - (entity._update_timeout + timedelta(seconds=1))

    await entity.async_update()

    assert entity._attr_available is False
    entity.schedule_update_ha_state.assert_called_once()


async def test_watchdog_keeps_fresh_sensor_available():
    entity = _entity(interval_s=5)
    entity.schedule_update_ha_state = MagicMock()
    entity._attr_available = True

    await entity.async_update()

    assert entity._attr_available is True
    entity.schedule_update_ha_state.assert_not_called()
