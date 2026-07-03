import logging
from abc import ABC, abstractmethod

from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.solis_modbus.const import DOMAIN, MANUFACTURER
from custom_components.solis_modbus.data.enums import PollSpeed
from custom_components.solis_modbus.data.solis_config import InverterConfig

_LOGGER = logging.getLogger(__name__)


class SolisControllerBase(ABC):
    """Protocol-agnostic controller surface shared by Modbus and SolisCloud implementations.

    Entities and retrieval loops only depend on this surface: they read values from the
    register cache scoped by `cache_scope` and write via `async_write_holding_register(s)`.
    Transport specifics (pymodbus clients, HTTP sessions) live in subclasses.
    """

    # Subclass __init__ is expected to set these; class-level defaults keep
    # lightweight test doubles working without a base __init__.
    hass = None
    serial_number: str | None = None
    identification: str | None = None
    device_id: int = 1
    enabled: bool = True
    inverter_config: InverterConfig | None = None
    _model: str = "Unknown"
    _sw_version: str = "N/A"
    _sensor_groups: list | None = None
    _derived_sensors: list | None = None

    @property
    def cache_scope(self) -> str:
        """Protocol-neutral scope for cache keys and dispatcher signals.

        The inverter serial is shared by every protocol talking to the same device, so
        both controllers land values on identical cache keys. The connection-based
        fallback only applies when no serial is configured (e.g. bare test controllers).
        """
        if self.serial_number:
            return str(self.serial_number)
        conn = getattr(self, "connection_id", None)
        if not isinstance(conn, str):
            conn = str(getattr(self, "host", "") or "")
        return f"{conn}|{int(self.device_id)}"

    @abstractmethod
    def connected(self) -> bool:
        """Whether the underlying transport is currently usable."""

    @abstractmethod
    async def async_write_holding_register(self, register, value):
        """Write a single register value to the inverter via this protocol."""

    @abstractmethod
    async def async_write_holding_registers(self, start_register, values):
        """Write consecutive register values to the inverter via this protocol."""

    @property
    @abstractmethod
    def poll_speed(self) -> dict[PollSpeed, int]:
        """Poll intervals (seconds) per speed category."""

    def disable_connection(self):
        self.enabled = False
        _LOGGER.info(f"({self.cache_scope}) connection disabled")

    def enable_connection(self):
        self.enabled = True
        _LOGGER.info(f"({self.cache_scope}) connection enabled")

    @property
    def model(self):
        """Returns the inverter model."""
        return self._model

    @property
    def sw_version(self):
        """Returns the software version of the inverter."""
        return self._sw_version

    @property
    def device_serial_number(self):
        """Gets the device serial number."""
        return self.serial_number

    @property
    def device_identification(self):
        """Gets the device identification string."""
        return self.identification

    def replace_sensor_group(self, old_group, new_groups: list) -> None:
        """Replace one sensor group with several (or none) at the same list index."""
        try:
            idx = self._sensor_groups.index(old_group)
        except ValueError:
            _LOGGER.warning("(%s) Sensor group to replace not found in controller list", self.cache_scope)
            return
        self._sensor_groups = self._sensor_groups[:idx] + new_groups + self._sensor_groups[idx + 1 :]

    @property
    def sensor_groups(self):
        """Returns the list of sensor groups."""
        return self._sensor_groups

    @property
    def derived_sensors(self):
        """Returns the list of derived sensors."""
        return self._derived_sensors

    @property
    def sensor_derived_groups(self):
        """Gets the derived sensor groups associated with this controller."""
        return self._derived_sensors

    @property
    def device_info(self):
        """Return device info."""
        name = f"{MANUFACTURER} {self.model}"

        return DeviceInfo(
            identifiers={(DOMAIN, self.serial_number)},
            manufacturer=MANUFACTURER,
            model=self.model,
            serial_number=self.serial_number,
            name=name,
            sw_version=self.sw_version,
        )
