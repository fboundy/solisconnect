import logging

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.solisconnect.const import DOMAIN, PROTOCOL_CLOUD, PROTOCOL_MODBUS

_LOGGER = logging.getLogger(__name__)

_OPTION_LABELS = {PROTOCOL_MODBUS: "Modbus", PROTOCOL_CLOUD: "SolisCloud"}
_LABEL_TO_PROTOCOL = {v: k for k, v in _OPTION_LABELS.items()}


class SolisProtocolSelect(RestoreEntity, SelectEntity):
    """Chooses which protocol actively polls and takes writes (manual dual mode only)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, hass, hub):
        self._hass = hass
        self._hub = hub
        self._attr_name = "Active Protocol"
        self._attr_unique_id = f"{DOMAIN}_{hub.serial_number}_active_protocol"
        self._attr_options = list(_OPTION_LABELS.values())
        self._attr_current_option = _OPTION_LABELS[hub.active_protocol]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state and state.state in _LABEL_TO_PROTOCOL:
            protocol = _LABEL_TO_PROTOCOL[state.state]
            if protocol != self._hub.active_protocol:
                await self._hub.async_set_active(protocol, reason="restored selection")
            self._attr_current_option = state.state

    async def async_select_option(self, option: str) -> None:
        await self._hub.async_set_active(_LABEL_TO_PROTOCOL[option], reason="user selection")
        self._attr_current_option = option
        self.schedule_update_ha_state()

    @property
    def device_info(self):
        return self._hub.device_info
