import asyncio
import logging
from datetime import UTC, datetime

from homeassistant.exceptions import HomeAssistantError

from custom_components.solisconnect.cloud.api_client import SolisCloudApiClient, SolisCloudApiError
from custom_components.solisconnect.const import DEFAULT_CLOUD_POLL_INTERVAL, MIN_CLOUD_POLL_INTERVAL
from custom_components.solisconnect.controller_base import SolisControllerBase
from custom_components.solisconnect.data.enums import PollSpeed
from custom_components.solisconnect.data.solis_config import InverterConfig
from custom_components.solisconnect.helpers import cache_save, notify_register_update
from custom_components.solisconnect.sensor_data.cloud_mapping import (
    REGISTER_TO_CID,
    CloudCidMapping,
    cid_msg_from_words,
    encode_cid_value,
)

_LOGGER = logging.getLogger(__name__)

# A controller is "connected" while its last successful poll is within this many intervals.
_CONNECTED_WINDOW_INTERVALS = 3

# Cloud state lags writes; wait this long before reading back to verify, and retry once.
_VERIFY_DELAY_SECONDS = 15


class SolisCloudController(SolisControllerBase):
    """Controller that feeds the shared register cache from the SolisCloud HTTP API.

    Presents the same surface as ModbusController (via SolisControllerBase) so entities
    and the hub don't care which protocol produced a value. Cloud writes land in Stage 5;
    until then write attempts raise a clear error.
    """

    def __init__(
        self,
        hass,
        inverter_config: InverterConfig,
        api: SolisCloudApiClient,
        plant_id: str,
        serial_number: str,
        identification=None,
        poll_interval: int = DEFAULT_CLOUD_POLL_INTERVAL,
        inverter_id: str | None = None,
        sensor_groups=None,
        derived_sensors=None,
    ):
        self.hass = hass
        self.inverter_config = inverter_config
        self._model = inverter_config.model
        self.api = api
        self.plant_id = plant_id
        self.serial_number = serial_number
        self.identification = identification
        self.device_id = 1
        self.enabled = True
        self.poll_interval = max(MIN_CLOUD_POLL_INTERVAL, int(poll_interval))
        self.inverter_id = inverter_id
        self._sensor_groups = sensor_groups or []
        self._derived_sensors = derived_sensors or []
        self.last_cloud_success: datetime | None = None

    @property
    def host(self) -> str:
        """Compatibility for log strings and dispatcher payloads."""
        return "soliscloud"

    def connected(self) -> bool:
        if self.last_cloud_success is None:
            return False
        age = (datetime.now(UTC) - self.last_cloud_success).total_seconds()
        return age < _CONNECTED_WINDOW_INTERVALS * self.poll_interval

    @property
    def poll_speed(self) -> dict[PollSpeed, int]:
        # Entities size their staleness watchdog as timedelta(minutes=poll_speed + 10),
        # so report the poll interval in minutes.
        minutes = max(1, self.poll_interval // 60)
        return {PollSpeed.FAST: minutes, PollSpeed.NORMAL: minutes, PollSpeed.SLOW: minutes}

    @property
    def last_modbus_success(self) -> datetime | None:
        """Name kept for parity with ModbusController consumers (pseudo-register 90006)."""
        return self.last_cloud_success

    def _mapping_for_write(self, registers: list[int]) -> CloudCidMapping:
        mapping = REGISTER_TO_CID.get(registers[0])
        if mapping is None or list(mapping.registers) != registers:
            raise HomeAssistantError(f"Register(s) {registers} cannot be written via SolisCloud (no control CID mapping)")
        return mapping

    async def async_write_holding_register(self, register, value):
        mapping = self._mapping_for_write([int(register)])
        await self._execute_cloud_write(mapping, [int(value)])

    async def async_write_holding_registers(self, start_register, values):
        registers = [int(start_register) + i for i in range(len(values))]
        mapping = self._mapping_for_write(registers)
        await self._execute_cloud_write(mapping, [int(v) for v in values])

    async def _execute_cloud_write(self, mapping: CloudCidMapping, words: list[int]):
        """Write a control CID: read old value -> control -> optimistic cache -> delayed verify."""
        msg = cid_msg_from_words(mapping, words)
        try:
            old_value = await self.api.async_at_read(self.serial_number, mapping.cid)
        except SolisCloudApiError as error:
            _LOGGER.debug("Pre-write read of cid %s failed (continuing without yuanzhi): %s", mapping.cid, error)
            old_value = None

        try:
            await self.api.async_control(self.serial_number, mapping.cid, msg, old_value=old_value)
        except SolisCloudApiError as error:
            raise HomeAssistantError(f"SolisCloud write of cid {mapping.cid} (registers {mapping.registers}) failed: {error}") from error

        _LOGGER.info("SolisCloud write: cid %s = %s (was %s)", mapping.cid, msg, old_value)

        # Optimistic: show the new value immediately; the verify task restores device truth on mismatch.
        for register, word in zip(mapping.registers, words):
            cache_save(self.hass, self, register, word)
            notify_register_update(self.hass, self, register, word)

        self.hass.async_create_task(self._verify_write(mapping, words))

    async def _verify_write(self, mapping: CloudCidMapping, expected_words: list[int]):
        """Re-read the CID after the cloud catches up; never leave an optimistic lie in the cache."""
        actual_words = None
        for _attempt in range(2):
            await asyncio.sleep(_VERIFY_DELAY_SECONDS)
            try:
                msg = await self.api.async_at_read(self.serial_number, mapping.cid)
            except SolisCloudApiError as error:
                _LOGGER.warning("Verify read of cid %s failed: %s", mapping.cid, error)
                continue
            decoded = encode_cid_value(mapping, msg)
            actual_words = [decoded[r] for r in mapping.registers] if decoded else None
            if actual_words == expected_words:
                _LOGGER.debug("SolisCloud write of cid %s verified", mapping.cid)
                return

        if actual_words is not None and actual_words != expected_words:
            _LOGGER.warning(
                "SolisCloud write of cid %s did not stick: wrote %s, device reports %s — restoring device value",
                mapping.cid,
                expected_words,
                actual_words,
            )
            for register, word in zip(mapping.registers, actual_words):
                cache_save(self.hass, self, register, word)
                notify_register_update(self.hass, self, register, word)

    def close_connection(self):
        """Nothing to close: the aiohttp session is Home Assistant's shared session."""
