import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.solisconnect import ModbusController
from custom_components.solisconnect.const import REGISTER, VALUE
from custom_components.solisconnect.helpers import (
    cache_get,
    is_correct_controller,
    register_update_signal,
    unique_id_generator_binary,
)
from custom_components.solisconnect.sensors.solis_binary_sensor import get_bit_bool

_LOGGER = logging.getLogger(__name__)


class SolisBinaryStatusSensor(RestoreEntity, BinarySensorEntity):
    """Read-only bit status. No write path - see SolisBinaryEntity for controllable switches."""

    def __init__(self, hass, modbus_controller, entity_definition):
        self._hass = hass
        self._modbus_controller: ModbusController = modbus_controller
        self._register: int = entity_definition.get("register", entity_definition.get("read_register")) + entity_definition.get("offset", 0)
        self._bit_position = entity_definition.get("bit_position", None)
        self._on_value = entity_definition.get("on_value", None)
        self._inverted = entity_definition.get("inverted", None)
        self._attr_unique_id = unique_id_generator_binary(modbus_controller, self._register, self._bit_position, self._on_value)
        self._attr_name = entity_definition["name"]
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Called when entity is added to HA."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_dispatcher_connect(
                self._hass,
                register_update_signal(self._modbus_controller, self._register),
                self.handle_modbus_update,
            )
        )

    @callback
    def handle_modbus_update(self, data):
        """Callback when register data is available (per-register dispatcher)."""
        updated_register = int(data.get(REGISTER))

        if not is_correct_controller(self._modbus_controller, data):
            return  # meant for a different sensor/inverter combo

        if updated_register != self._register:
            return

        updated_value = data.get(VALUE)
        value = int(updated_value) if updated_value is not None else cache_get(self._hass, self._modbus_controller, self._register)
        if value is None:
            return

        self._attr_available = True
        if self._bit_position is not None:
            raw_bit = get_bit_bool(value, self._bit_position)
            self._attr_is_on = (not raw_bit) if self._inverted else raw_bit
        elif self._on_value is not None:
            self._attr_is_on = value == self._on_value

        self.schedule_update_ha_state()

    @property
    def is_on(self):
        return self._attr_is_on

    @property
    def device_info(self):
        """Return device info."""
        return self._modbus_controller.device_info
