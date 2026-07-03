import asyncio
import logging
from datetime import UTC, datetime, timedelta

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from custom_components.solisconnect.const import (
    PROTO_BOTH_FAILOVER,
    PROTO_BOTH_MANUAL,
    PROTO_CLOUD_ONLY,
    PROTOCOL_CLOUD,
    PROTOCOL_MODBUS,
)
from custom_components.solisconnect.controller_base import SolisControllerBase
from custom_components.solisconnect.data.solis_config import InverterConfig
from custom_components.solisconnect.helpers import notify_register_update

_LOGGER = logging.getLogger(__name__)

# Failover tuning
HEALTH_CHECK_INTERVAL_SECONDS = 30
MODBUS_UNHEALTHY_AGE_SECONDS = 120  # active Modbus with no successful read for this long is dead
PRIMARY_RECOVERY_SECONDS = 300  # primary must probe healthy for this long before auto-return
MIN_DWELL_SECONDS = 120  # never switch again sooner than this (anti-flap)

# Pseudo-register announcing the active protocol (precedent: 90005/90006/90007)
ACTIVE_PROTOCOL_REGISTER = 90008


class SolisInverterHub(SolisControllerBase):
    """One inverter, one or two transports.

    Entities bind to the hub (their `controller`), so they survive protocol switches:
    the hub delegates reads/writes/state to whichever concrete controller is active.
    All controllers for one inverter share the same serial-based `cache_scope`, so
    cache keys and dispatcher signals are identical regardless of the producer.

    Exactly ONE protocol polls at a time (the standby stays idle); switching stops the
    active retrieval, starts the other, and triggers an immediate refresh. In failover
    mode a periodic health check switches away from a dead primary and auto-returns
    once the primary has been continuously healthy (with anti-flap dwell times).
    """

    def __init__(self, hass, entry_id: str, inverter_config: InverterConfig, serial_number: str, mode: str, identification=None, failover_primary=None):
        self.hass = hass
        self.entry_id = entry_id
        self.inverter_config = inverter_config
        self._model = inverter_config.model
        self.serial_number = serial_number
        self.identification = identification
        self.mode = mode
        self.modbus = None  # ModbusController | None
        self.cloud = None  # SolisCloudController | None

        self.failover_primary = failover_primary or PROTOCOL_MODBUS
        self._active_name = PROTOCOL_CLOUD if mode == PROTO_CLOUD_ONLY else (self.failover_primary if mode == PROTO_BOTH_FAILOVER else PROTOCOL_MODBUS)
        self._retrieval = None
        self._unsub_health = None
        self._last_switch = datetime.min.replace(tzinfo=UTC)
        self._primary_healthy_since: datetime | None = None
        # Serializes protocol switches: _health_tick, the protocol select entity, and its
        # restore path can all call async_set_active concurrently.
        self._switch_lock = asyncio.Lock()
        # Guards against overlapping _health_tick runs: a probe of a dead standby link
        # (connect() + read) can take longer than HEALTH_CHECK_INTERVAL_SECONDS, and
        # async_track_time_interval fires on schedule regardless of whether the previous
        # callback finished, so a second tick could otherwise race the first's in-flight
        # probe and its (unlocked) _primary_healthy_since/_last_switch bookkeeping.
        self._health_tick_running = False

    # --- lifecycle -----------------------------------------------------------------

    async def async_start(self):
        """Start the active protocol's retrieval (and the failover health check)."""
        self._start_retrieval(self._active_name)
        if self.mode == PROTO_BOTH_FAILOVER:
            self._unsub_health = async_track_time_interval(self.hass, self._health_tick, timedelta(seconds=HEALTH_CHECK_INTERVAL_SECONDS))

    async def async_stop(self):
        if self._unsub_health is not None:
            self._unsub_health()
            self._unsub_health = None
        if self._retrieval is not None:
            await self._retrieval.async_stop()
            self._retrieval = None

    def _controller_for(self, protocol: str):
        return self.modbus if protocol == PROTOCOL_MODBUS else self.cloud

    def _start_retrieval(self, protocol: str):
        if self._controller_for(protocol) is None:
            raise HomeAssistantError(f"Protocol {protocol} is not configured for this entry")
        if protocol == PROTOCOL_MODBUS:
            from custom_components.solisconnect.data_retrieval import DataRetrieval

            self._retrieval = DataRetrieval(self.hass, self.modbus, hub=self)
        else:
            from custom_components.solisconnect.cloud.cloud_retrieval import CloudDataRetrieval

            # In dual modes Modbus can feed the unmapped registers later, so don't disable them
            self._retrieval = CloudDataRetrieval(self.hass, self.cloud, disable_unmapped=self.mode == PROTO_CLOUD_ONLY)

    async def async_set_active(self, protocol: str, reason: str):
        """Switch the polling/writing protocol. No-op if it is already active."""
        if self._controller_for(protocol) is None:
            raise HomeAssistantError(f"Protocol {protocol} is not configured for this entry")

        async with self._switch_lock:
            if protocol == self._active_name:
                return

            _LOGGER.warning("(%s) Switching active protocol %s -> %s (%s)", self.cache_scope, self._active_name, protocol, reason)
            if self._retrieval is not None:
                await self._retrieval.async_stop()
                self._retrieval = None

            self._active_name = protocol
            self._last_switch = datetime.now(UTC)
            self._primary_healthy_since = None
            self._start_retrieval(protocol)
        notify_register_update(self.hass, self, ACTIVE_PROTOCOL_REGISTER, protocol)

    # --- failover ------------------------------------------------------------------

    def _is_active_healthy(self) -> bool:
        active = self.active
        if active is self.modbus:
            last = getattr(active, "last_modbus_success", None)
            if last is None:
                return False
            return (datetime.now(UTC) - last).total_seconds() < MODBUS_UNHEALTHY_AGE_SECONDS
        return active.connected()

    async def _probe_primary(self) -> bool:
        """Quietly check the (standby) primary without publishing anything."""
        primary = self._controller_for(self.failover_primary)
        try:
            if primary is self.modbus:
                if not await primary.connect():
                    return False
                registers = await primary.async_read_input_register(35000, 1)
                return registers is not None
            await primary.api.async_at_read(primary.serial_number, 636)
            return True
        except Exception as error:
            _LOGGER.debug("(%s) Primary probe failed: %s", self.cache_scope, error)
            return False

    async def _health_tick(self, now=None):
        if self._health_tick_running:
            _LOGGER.debug("(%s) Skipping health tick: a previous probe is still running", self.cache_scope)
            return
        self._health_tick_running = True
        try:
            dwell = (datetime.now(UTC) - self._last_switch).total_seconds()
            secondary = PROTOCOL_CLOUD if self.failover_primary == PROTOCOL_MODBUS else PROTOCOL_MODBUS

            if self._active_name == self.failover_primary:
                if not self._is_active_healthy() and dwell >= MIN_DWELL_SECONDS and self._controller_for(secondary) is not None:
                    await self.async_set_active(secondary, reason="primary unhealthy")
                return

            # Running on the secondary: probe the primary and auto-return with hysteresis
            if await self._probe_primary():
                if self._primary_healthy_since is None:
                    self._primary_healthy_since = datetime.now(UTC)
                healthy_for = (datetime.now(UTC) - self._primary_healthy_since).total_seconds()
                if healthy_for >= PRIMARY_RECOVERY_SECONDS and dwell >= MIN_DWELL_SECONDS:
                    await self.async_set_active(self.failover_primary, reason="primary recovered")
            else:
                self._primary_healthy_since = None
        finally:
            self._health_tick_running = False

    # --- delegated surface (entities and services talk to the hub) ---

    @property
    def active(self) -> SolisControllerBase:
        """The controller currently feeding the cache and taking writes."""
        return self._controller_for(self._active_name)

    @property
    def active_protocol(self) -> str:
        return self._active_name

    @property
    def host(self):
        return self.active.host

    @property
    def device_id(self):
        return self.active.device_id

    @property
    def enabled(self):
        return self.active.enabled

    def connected(self) -> bool:
        return self.active.connected()

    @property
    def poll_speed(self):
        return self.active.poll_speed

    @property
    def sensor_groups(self):
        return self.active.sensor_groups

    @property
    def derived_sensors(self):
        return self.active.derived_sensors

    @property
    def sensor_derived_groups(self):
        return self.active.sensor_derived_groups

    @property
    def last_modbus_success(self):
        return self.active.last_modbus_success

    def replace_sensor_group(self, old_group, new_groups):
        """Apply a recovery edit to every configured controller, not just the active one.

        Modbus and cloud controllers are handed the same sensor-group list objects at
        setup, but replace_sensor_group reassigns `_sensor_groups` to a new list on
        whichever controller instance it's called on — so applying it to only the
        active controller silently loses the recovered layout on the standby protocol
        once a switch happens.
        """
        for controller in (self.modbus, self.cloud):
            if controller is not None:
                controller.replace_sensor_group(old_group, new_groups)

    def enable_connection(self):
        for controller in (self.modbus, self.cloud):
            if controller is not None:
                controller.enable_connection()

    def disable_connection(self):
        for controller in (self.modbus, self.cloud):
            if controller is not None:
                controller.disable_connection()

    def close_connection(self):
        for controller in (self.modbus, self.cloud):
            if controller is not None:
                controller.close_connection()

    async def async_write_holding_register(self, register, value):
        await self._routed_write("async_write_holding_register", register, value)

    async def async_write_holding_registers(self, start_register, values):
        await self._routed_write("async_write_holding_registers", start_register, values)

    async def _routed_write(self, method: str, *args):
        """Write via the active protocol; in dual modes fall back to the other if it cannot."""
        try:
            await getattr(self.active, method)(*args)
            return
        except HomeAssistantError as error:
            other_name = PROTOCOL_CLOUD if self._active_name == PROTOCOL_MODBUS else PROTOCOL_MODBUS
            other = self._controller_for(other_name) if self.mode in (PROTO_BOTH_FAILOVER, PROTO_BOTH_MANUAL) else None
            if other is None or not other.connected():
                raise
            _LOGGER.warning("(%s) Write via %s failed (%s); retrying via %s", self.cache_scope, self._active_name, error, other_name)
            await getattr(other, method)(*args)
