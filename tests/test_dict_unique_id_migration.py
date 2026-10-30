"""Sensor/number unique IDs used to embed the repr of the whole definition dict; setup renames them."""

from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solisconnect import migrate_dict_unique_ids
from custom_components.solisconnect.const import DOMAIN

SERIAL = "SN1"
CONTROLLER = SimpleNamespace(device_serial_number=SERIAL, identification=None, host="1.2.3.4")
NEW_UID = f"{DOMAIN}_{SERIAL}_solisconnect_inverter_dsp_version"


def _legacy_uid(definition: dict) -> str:
    """What the pre-fix code produced: unique_id_generator(controller, <definition dict>)."""
    return f"{DOMAIN}_{SERIAL}_{definition}"


DEFINITION = {"name": "DSP Version", "unique": "solisconnect_inverter_dsp_version", "register": ["33001"], "multiplier": 0}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL)
    entry.add_to_hass(hass)
    return entry


async def test_renames_legacy_id_and_keeps_entity_id(hass: HomeAssistant):
    entry = _entry(hass)
    ent_reg = er.async_get(hass)
    sensor = ent_reg.async_get_or_create("sensor", DOMAIN, _legacy_uid(DEFINITION), config_entry=entry, suggested_object_id="solis_dsp_version")
    number = ent_reg.async_get_or_create("number", DOMAIN, _legacy_uid(DEFINITION), config_entry=entry, suggested_object_id="solis_dsp_number")

    migrate_dict_unique_ids(hass, entry, CONTROLLER)

    assert ent_reg.async_get(sensor.entity_id).unique_id == NEW_UID
    assert ent_reg.async_get(number.entity_id).unique_id == NEW_UID


async def test_collapses_ids_from_several_definition_edits_onto_the_oldest(hass: HomeAssistant):
    """Each edit to a definition (e.g. adding value_format) orphaned the old entity and created a new one."""
    entry = _entry(hass)
    ent_reg = er.async_get(hass)
    original = ent_reg.async_get_or_create("sensor", DOMAIN, _legacy_uid(DEFINITION), config_entry=entry, suggested_object_id="solis_dsp_version")
    edited = {**DEFINITION, "value_format": "hex"}
    duplicate = ent_reg.async_get_or_create("sensor", DOMAIN, _legacy_uid(edited), config_entry=entry, suggested_object_id="solis_dsp_version_2")

    migrate_dict_unique_ids(hass, entry, CONTROLLER)

    assert ent_reg.async_get(original.entity_id).unique_id == NEW_UID
    assert ent_reg.async_get(duplicate.entity_id) is None


async def test_leaves_current_ids_alone_and_is_idempotent(hass: HomeAssistant):
    entry = _entry(hass)
    ent_reg = er.async_get(hass)
    current = ent_reg.async_get_or_create("sensor", DOMAIN, NEW_UID, config_entry=entry)
    switch = ent_reg.async_get_or_create("switch", DOMAIN, f"{DOMAIN}_{SERIAL}_43110_0", config_entry=entry)

    migrate_dict_unique_ids(hass, entry, CONTROLLER)
    migrate_dict_unique_ids(hass, entry, CONTROLLER)

    assert ent_reg.async_get(current.entity_id).unique_id == NEW_UID
    assert ent_reg.async_get(switch.entity_id).unique_id == f"{DOMAIN}_{SERIAL}_43110_0"
