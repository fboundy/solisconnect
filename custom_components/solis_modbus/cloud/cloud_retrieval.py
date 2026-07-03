import logging
from datetime import UTC, datetime, timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from custom_components.solis_modbus.cloud.api_client import SolisCloudApiError
from custom_components.solis_modbus.cloud.cloud_controller import SolisCloudController
from custom_components.solis_modbus.helpers import (
    cache_save,
    mark_platform_entities_unavailable_for_base_sensors,
    notify_register_update,
)
from custom_components.solis_modbus.sensor_data.cloud_mapping import (
    CLOUD_CID_MAP,
    CLOUD_INPUT_MAP,
    encode_cid_value,
    encode_engineering_value,
    registers_covered,
)

_LOGGER = logging.getLogger(__name__)


class CloudDataRetrieval:
    """Polls the SolisCloud API and lands raw register words in the shared cache.

    Mirrors DataRetrieval's lifecycle (start on HA startup, periodic timer, stop on unload)
    but with a single poll interval: one inverterDetail call covers the input registers and
    one atReadBatch covers the mapped control CIDs. All rate limiting lives in the API client.
    """

    def __init__(self, hass: HomeAssistant, controller: SolisCloudController, disable_unmapped: bool = True):
        self.hass = hass
        self.controller = controller
        self._disable_unmapped = disable_unmapped
        self.poll_updating = False
        self._unsub_timer = None
        self._unsub_start = None

        if hass.is_running:
            hass.async_create_task(self.poll_controller())
        else:
            self._unsub_start = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self.poll_controller)

    async def poll_controller(self, event=None):
        """First poll plus the periodic timer."""
        self._unsub_start = None
        if self._disable_unmapped:
            self._disable_unmapped_sensors()
        await self._ensure_inverter_id()
        await self._update_cycle()
        self._unsub_timer = async_track_time_interval(self.hass, self._update_cycle, timedelta(seconds=self.controller.poll_interval))

    async def _ensure_inverter_id(self):
        """inverterDetail needs the cloud id alongside the serial; entries created before the
        id was stored (or hand-edited ones) self-heal by looking it up once."""
        controller = self.controller
        if controller.inverter_id:
            return
        try:
            records = await controller.api.async_inverter_list(controller.plant_id)
        except SolisCloudApiError as error:
            _LOGGER.warning("SolisCloud inverter id lookup failed (will retry next cycle): %s", error)
            return
        for record in records:
            if str(record.get("sn", "")).upper() == str(controller.serial_number).upper():
                controller.inverter_id = str(record.get("id") or "") or None
                _LOGGER.info("Resolved SolisCloud inverter id %s for serial %s", controller.inverter_id, controller.serial_number)
                return
        _LOGGER.warning("Serial %s not found in SolisCloud plant %s", controller.serial_number, controller.plant_id)

    async def _update_cycle(self, now=None):
        if self.poll_updating:
            _LOGGER.debug("Cloud poll skipped: previous cycle still running")
            return
        if not self.controller.enabled:
            self._publish_status()
            return

        self.poll_updating = True
        try:
            controller = self.controller
            if not controller.inverter_id:
                await self._ensure_inverter_id()
            detail = await controller.api.async_inverter_detail(controller.serial_number, controller.inverter_id)
            published = self._publish_detail(detail)
            if published == 0:
                _LOGGER.warning(
                    "SolisCloud inverterDetail yielded no mappable registers (%s fields in payload, inverter_id=%s) — sensors will not update",
                    len(detail) if isinstance(detail, dict) else "?",
                    controller.inverter_id,
                )

            cid_words = 0
            batch = await controller.api.async_at_read_batch(controller.serial_number, list(CLOUD_CID_MAP))
            for cid, msg in batch.items():
                mapping = CLOUD_CID_MAP.get(cid)
                if mapping is None:
                    continue  # the API returns extra related CIDs beyond those requested
                words = encode_cid_value(mapping, msg)
                if words:
                    self._publish_words(words)
                    cid_words += len(words)

            controller.last_cloud_success = datetime.now(UTC)
            # INFO so it is visible in `ha core logs` when diagnosing live installs
            _LOGGER.info("Cloud poll complete: %s detail registers, %s cid registers", published, cid_words)
        except SolisCloudApiError as error:
            _LOGGER.warning("SolisCloud poll failed: %s", error)
        finally:
            self._publish_status()
            self.poll_updating = False

    def _publish_detail(self, detail: dict) -> int:
        published = 0
        for mapping in CLOUD_INPUT_MAP.values():
            value = detail.get(mapping.api_field)
            if value is None:
                continue
            if mapping.encoder is not None:
                try:
                    words = mapping.encoder(value, detail)
                except (TypeError, ValueError):
                    _LOGGER.debug("Cloud field %s=%r not encodable", mapping.api_field, value)
                    continue
            else:
                words = encode_engineering_value(mapping, value, detail.get(mapping.unit_field) if mapping.unit_field else None)
            if words:
                self._publish_words(words)
                published += len(words)
        return published

    def _publish_words(self, words: dict[int, int]):
        for register, word in words.items():
            cache_save(self.hass, self.controller, register, word)
            notify_register_update(self.hass, self.controller, register, word)

    def _publish_status(self):
        """Pseudo-registers used by diagnostic derived sensors (same as DataRetrieval)."""
        notify_register_update(self.hass, self.controller, 90005, self.controller.enabled)
        if self.controller.last_cloud_success is not None:
            notify_register_update(self.hass, self.controller, 90006, self.controller.last_cloud_success)

    def _disable_unmapped_sensors(self):
        """In cloud-only mode, registers with no cloud mapping never update: make their
        entities unavailable immediately instead of leaving restored/stale values."""
        covered = registers_covered()
        disabled = []
        for group in self.controller.sensor_groups or []:
            for sensor in group.sensors:
                if sensor.name == "reserve" or not sensor.enabled:
                    continue
                if not all(register in covered for register in sensor.registrars):
                    sensor.enabled = False
                    disabled.append(sensor)
        if disabled:
            _LOGGER.info("Cloud-only mode: %s sensors have no SolisCloud mapping and will be unavailable", len(disabled))
            mark_platform_entities_unavailable_for_base_sensors(self.hass, disabled)

    async def async_stop(self):
        if self._unsub_start is not None:
            self._unsub_start()
            self._unsub_start = None
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
