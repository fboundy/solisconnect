import asyncio
import logging
from datetime import UTC, datetime

from homeassistant.exceptions import HomeAssistantError

from custom_components.solisconnect.cloud.api_client import SolisCloudApiClient, SolisCloudApiError
from custom_components.solisconnect.const import DEFAULT_CLOUD_POLL_INTERVAL, MIN_CLOUD_POLL_INTERVAL
from custom_components.solisconnect.controller_base import SolisControllerBase
from custom_components.solisconnect.data.enums import PollSpeed
from custom_components.solisconnect.data.solis_config import InverterConfig
from custom_components.solisconnect.helpers import cache_get, cache_save, notify_register_update
from custom_components.solisconnect.sensor_data.cloud_mapping import (
    REGISTER_TO_CID,
    TOU_SWITCH_CIDS_BY_BIT,
    TOU_SWITCH_REGISTER,
    TOU_TIME_PAIR_TO_CID,
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
        # Delayed write-verify tasks; cancelled on close_connection so they cannot
        # touch the cache after the entry is unloaded.
        self._verify_tasks: set[asyncio.Task] = set()

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
        # Same semantics as ModbusController: poll intervals in seconds.
        return {PollSpeed.FAST: self.poll_interval, PollSpeed.NORMAL: self.poll_interval, PollSpeed.SLOW: self.poll_interval}

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
        if int(register) == TOU_SWITCH_REGISTER:
            await self._execute_tou_switch_write(int(value))
            return
        mapping = self._mapping_for_write([int(register)])
        await self._execute_cloud_write(mapping, [int(value)])

    async def async_write_holding_registers(self, start_register, values):
        registers = [int(start_register) + i for i in range(len(values))]
        time_mapping = TOU_TIME_PAIR_TO_CID.get(tuple(registers))
        if time_mapping is not None:
            await self._execute_tou_time_write(time_mapping, registers, [int(v) for v in values])
            return
        mapping = self._mapping_for_write(registers)
        await self._execute_cloud_write(mapping, [int(v) for v in values])

    async def _execute_cloud_write(self, mapping: CloudCidMapping, words: list[int]):
        """Write a control CID: read old value -> control -> optimistic cache -> delayed verify."""
        msg = cid_msg_from_words(mapping, words)
        if msg is None:
            raise HomeAssistantError(f"Register(s) {mapping.registers} cannot be encoded for SolisCloud cid {mapping.cid}")
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

        self._start_verify_task(self._verify_write(mapping, words))

    async def _execute_tou_time_write(self, mapping: CloudCidMapping, write_registers: list[int], write_words: list[int]):
        """Write one start/end HA time pair as the cloud API's full HH:MM-HH:MM slot string."""
        current_words = await self._current_words_for_mapping(mapping)
        if current_words is None:
            raise HomeAssistantError(f"SolisCloud write of cid {mapping.cid} failed: current time slot value is unavailable")

        index_by_register = {register: index for index, register in enumerate(mapping.registers)}
        for register, word in zip(write_registers, write_words):
            current_words[index_by_register[register]] = word

        await self._execute_cloud_write(mapping, current_words)

    async def _current_words_for_mapping(self, mapping: CloudCidMapping) -> list[int] | None:
        cached = [cache_get(self.hass, self, register) for register in mapping.registers]
        if all(value is not None for value in cached):
            return [int(value) for value in cached]

        try:
            msg = await self.api.async_at_read(self.serial_number, mapping.cid)
        except SolisCloudApiError as error:
            _LOGGER.debug("Read of cid %s for composed write failed: %s", mapping.cid, error)
            return None
        decoded = encode_cid_value(mapping, msg)
        if decoded is None:
            return None
        return [decoded[register] for register in mapping.registers]

    async def _execute_tou_switch_write(self, new_value: int):
        """Write register 43707 by sending changed TOU V2 switch CIDs with old bitfield state."""
        new_value &= 0x0FFF
        old_value = cache_get(self.hass, self, TOU_SWITCH_REGISTER)
        if old_value is not None:
            old_value = int(old_value)
        else:
            try:
                old_value = await self._read_tou_switch_bitfield()
            except SolisCloudApiError as error:
                raise HomeAssistantError(f"SolisCloud write of TOU switch register {TOU_SWITCH_REGISTER} failed: {error}") from error

        changes = [bit for bit in sorted(TOU_SWITCH_CIDS_BY_BIT) if ((old_value >> bit) & 1) != ((new_value >> bit) & 1)]
        if not changes:
            cache_save(self.hass, self, TOU_SWITCH_REGISTER, new_value)
            notify_register_update(self.hass, self, TOU_SWITCH_REGISTER, new_value)
            return

        running_old_value = old_value
        try:
            for bit in changes:
                cid = TOU_SWITCH_CIDS_BY_BIT[bit]
                desired = "1" if ((new_value >> bit) & 1) else "0"
                await self.api.async_control(self.serial_number, cid, desired, old_value=str(running_old_value))
                if desired == "1":
                    running_old_value |= 1 << bit
                else:
                    running_old_value &= ~(1 << bit)
        except SolisCloudApiError as error:
            raise HomeAssistantError(f"SolisCloud write of TOU switch register {TOU_SWITCH_REGISTER} failed: {error}") from error

        _LOGGER.info("SolisCloud write: TOU switch register %s = %s (was %s)", TOU_SWITCH_REGISTER, new_value, old_value)
        cache_save(self.hass, self, TOU_SWITCH_REGISTER, new_value)
        notify_register_update(self.hass, self, TOU_SWITCH_REGISTER, new_value)
        self._start_verify_task(self._verify_tou_switch_write(new_value))

    async def _read_tou_switch_bitfield(self) -> int:
        batch = await self.api.async_at_read_batch(self.serial_number, list(TOU_SWITCH_CIDS_BY_BIT.values()))
        value = 0
        for bit, cid in TOU_SWITCH_CIDS_BY_BIT.items():
            try:
                if int(str(batch.get(cid, "0")).strip()):
                    value |= 1 << bit
            except ValueError:
                continue
        return value

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

    async def _verify_tou_switch_write(self, expected_value: int):
        """Verify the composed TOU switch bitfield by reading the twelve switch CIDs."""
        actual_value = None
        for _attempt in range(2):
            await asyncio.sleep(_VERIFY_DELAY_SECONDS)
            try:
                actual_value = await self._read_tou_switch_bitfield()
            except SolisCloudApiError as error:
                _LOGGER.warning("Verify read of TOU switch CIDs failed: %s", error)
                continue
            if actual_value == expected_value:
                _LOGGER.debug("SolisCloud write of TOU switch register %s verified", TOU_SWITCH_REGISTER)
                return

        if actual_value is not None and actual_value != expected_value:
            _LOGGER.warning(
                "SolisCloud write of TOU switch register %s did not stick: wrote %s, device reports %s — restoring device value",
                TOU_SWITCH_REGISTER,
                expected_value,
                actual_value,
            )
            cache_save(self.hass, self, TOU_SWITCH_REGISTER, actual_value)
            notify_register_update(self.hass, self, TOU_SWITCH_REGISTER, actual_value)

    async def async_read_holding_register(self, register, count=1):
        """Read from the cloud-backed cache, with a live TOU switch read for cold-cache bit toggles."""
        register = int(register)
        count = int(count)
        if register == TOU_SWITCH_REGISTER and count == 1:
            try:
                value = await self._read_tou_switch_bitfield()
            except SolisCloudApiError as error:
                _LOGGER.warning("Read of TOU switch CIDs failed: %s", error)
                return None
            cache_save(self.hass, self, register, value)
            notify_register_update(self.hass, self, register, value)
            return [value]
        values = [cache_get(self.hass, self, register + offset) for offset in range(count)]
        if any(value is None for value in values):
            return None
        return [int(value) for value in values]

    def _start_verify_task(self, coro):
        """Run a delayed verify as a tracked task so close_connection can cancel it."""
        task = self.hass.async_create_task(coro)
        self._verify_tasks.add(task)
        task.add_done_callback(self._verify_tasks.discard)
        return task

    def close_connection(self):
        """Cancel outstanding verify tasks; the aiohttp session is Home Assistant's shared session."""
        for task in list(self._verify_tasks):
            task.cancel()
        self._verify_tasks.clear()
