from custom_components.solisconnect.data.enums import InverterType


def get_binary_sensors(inverter_config):
    """Read-only bit-based status sensors (no write path, unlike switch_sensors.py)."""
    binary_sensors = []

    if inverter_config.type == InverterType.HYBRID:
        binary_sensors = [
            {
                "register": 43110,
                "entities": [
                    # Bit 1 of the Energy Storage Control Switch does not persist as a
                    # user-set value on this firmware family: writing it succeeds and is
                    # acknowledged, but the device silently clears it again within one poll
                    # cycle while leaving every other bit untouched. Confirmed live and
                    # cross-referenced against https://github.com/wills106/homeassistant-solax-modbus
                    # (plugin_solis_fb00.py), which never exposes this bit as a controllable
                    # option either. Real TOU enablement lives entirely in the per-slot
                    # switches on register 43707. Exposed here read-only for visibility only.
                    {"bit_position": 1, "name": "Timed Charge Status"},
                ],
            },
        ]

    return binary_sensors
