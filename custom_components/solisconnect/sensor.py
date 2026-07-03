import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from custom_components.solisconnect import ModbusController
from custom_components.solisconnect.const import DOMAIN, SENSOR_DERIVED_ENTITIES, SENSOR_ENTITIES, VALUES
from custom_components.solisconnect.helpers import get_controller_from_entry
from custom_components.solisconnect.sensors.solis_derived_sensor import SolisDerivedSensor
from custom_components.solisconnect.sensors.solis_sensor import SolisSensor

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up Modbus sensors from a config entry."""
    controller: ModbusController = get_controller_from_entry(hass, config_entry)
    sensor_entities: list[SolisSensor] = []
    sensor_derived_entities: list[SensorEntity] = []
    hass.data.setdefault(DOMAIN, {})
    # The cache and entity lists are shared across config entries — never reset them here,
    # or a second entry's setup wipes the first entry's values.
    hass.data[DOMAIN].setdefault(VALUES, {})

    for sensor_group in controller.sensor_groups:
        for sensor in sensor_group.sensors:
            if sensor.name != "reserve":
                sensor_entities.append(SolisSensor(hass, sensor))

    for sensor in controller.derived_sensors:
        sensor_derived_entities.append(SolisDerivedSensor(hass, sensor))

    hass.data[DOMAIN].setdefault(SENSOR_ENTITIES, []).extend(sensor_entities)
    hass.data[DOMAIN].setdefault(SENSOR_DERIVED_ENTITIES, []).extend(sensor_derived_entities)

    async_add_entities(sensor_entities, True)
    async_add_entities(sensor_derived_entities, True)

    @callback
    def update(now):
        """Update Modbus data periodically."""

    return True
