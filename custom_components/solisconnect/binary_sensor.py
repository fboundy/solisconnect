import logging

from homeassistant.config_entries import ConfigEntry

from custom_components.solisconnect import ModbusController
from custom_components.solisconnect.const import BINARY_SENSOR_ENTITIES, DOMAIN, ENTITIES
from custom_components.solisconnect.helpers import get_controller_from_entry
from custom_components.solisconnect.sensor_data.binary_sensors import get_binary_sensors
from custom_components.solisconnect.sensors.solis_binary_status_sensor import SolisBinaryStatusSensor

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    modbus_controller: ModbusController = get_controller_from_entry(hass, config_entry)

    binary_groups = get_binary_sensors(modbus_controller.inverter_config)

    binary_entities: list[SolisBinaryStatusSensor] = []

    for main_entity in binary_groups:
        for child_entity in main_entity[ENTITIES]:
            child_entity["register"] = main_entity.get("register", main_entity.get("read_register"))
            binary_entities.append(SolisBinaryStatusSensor(hass, modbus_controller, child_entity))

    hass.data[DOMAIN].setdefault(BINARY_SENSOR_ENTITIES, []).extend(binary_entities)
    async_add_devices(binary_entities, True)

    return True
