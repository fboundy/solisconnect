"""Diagnostics support for SolisConnect."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.solisconnect.const import CONTROLLER, DOMAIN
from custom_components.solisconnect.helpers import redact_config
from custom_components.solisconnect.sensor_data.cloud_mapping import CLOUD_CID_MAP, CLOUD_INPUT_MAP, registers_covered


def _controller_diagnostics(controller) -> dict:
    data = {
        "model": getattr(controller, "model", None),
        "serial_number": getattr(controller, "serial_number", None) or getattr(controller, "device_serial_number", None),
        "cache_scope": getattr(controller, "cache_scope", None),
        "connected": controller.connected() if hasattr(controller, "connected") else None,
    }

    active_protocol = getattr(controller, "active_protocol", None)
    if isinstance(active_protocol, str):
        data.update(
            {
                "mode": getattr(controller, "mode", None),
                "active_protocol": active_protocol,
                "failover_primary": getattr(controller, "failover_primary", None),
                "has_modbus": getattr(controller, "modbus", None) is not None,
                "has_cloud": getattr(controller, "cloud", None) is not None,
            }
        )
        if getattr(controller, "cloud", None) is not None:
            data["cloud"] = _controller_diagnostics(controller.cloud)
        if getattr(controller, "modbus", None) is not None:
            data["modbus"] = _controller_diagnostics(controller.modbus)
        return data

    poll_interval = getattr(controller, "poll_interval", None)
    if isinstance(poll_interval, int):
        data.update(
            {
                "plant_id": getattr(controller, "plant_id", None),
                "inverter_id_present": bool(getattr(controller, "inverter_id", None)),
                "last_cloud_success": str(getattr(controller, "last_cloud_success", None)),
                "poll_interval": poll_interval,
            }
        )
    else:
        data.update(
            {
                "host": getattr(controller, "host", None),
                "connection_type": getattr(controller, "connection_type", None),
                "connection_id": getattr(controller, "connection_id", None),
                "last_modbus_success": str(getattr(controller, "last_modbus_success", None)),
            }
        )
    return data


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Return diagnostics for a config entry."""
    controller = hass.data.get(DOMAIN, {}).get(CONTROLLER, {}).get(entry.entry_id)
    covered = registers_covered()

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "domain": entry.domain,
            "title": entry.title,
            "unique_id_present": bool(entry.unique_id),
            "data": redact_config(dict(entry.data)),
            "options": redact_config(dict(entry.options)),
        },
        "controller": _controller_diagnostics(controller) if controller is not None else None,
        "cloud_mapping": {
            "detail_fields": len(CLOUD_INPUT_MAP),
            "control_cids": len(CLOUD_CID_MAP),
            "covered_registers": len(covered),
        },
    }
