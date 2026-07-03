from unittest.mock import MagicMock

from homeassistant.helpers.entity import EntityCategory

from custom_components.solisconnect.data.enums import Category, InverterFeature
from custom_components.solisconnect.sensors.solis_base_sensor import SolisBaseSensor
from custom_components.solisconnect.sensors.solis_binary_sensor import SolisBinaryEntity
from custom_components.solisconnect.sensors.solis_number_sensor import SolisNumberEntity
from custom_components.solisconnect.sensors.solis_select_entity import SolisSelectEntity
from custom_components.solisconnect.sensors.solis_sensor import SolisSensor
from custom_components.solisconnect.time import SolisTimeEntity


def _controller():
    controller = MagicMock()
    controller.inverter_config.model = "S6-EH1P"
    controller.inverter_config.features = [InverterFeature.PV]
    controller.inverter_config.wattage_chosen = 5000
    controller.poll_speed = {}
    return controller


def test_hmi_and_dsp_version_registers_format_as_hex():
    controller = _controller()
    sensor = SolisBaseSensor(
        hass=MagicMock(),
        controller=controller,
        unique_id="version",
        name="HMI Version",
        registrars=[33002],
        write_register=None,
        multiplier=0,
        value_format="hex",
    )

    assert sensor.convert_value([0x4B00]) == "0x4B00"
    assert sensor.convert_value([1]) == "0x0001"


def test_setting_category_sensor_is_configuration_entity(hass):
    sensor = MagicMock(spec=SolisBaseSensor)
    sensor.controller = _controller()
    sensor.name = "RC Timeout"
    sensor.registrars = [43282]
    sensor.write_register = 43282
    sensor.device_class = None
    sensor.unit_of_measurement = None
    sensor.state_class = None
    sensor.multiplier = 1
    sensor.enabled = True
    sensor.hidden = False
    sensor.unique_id = "rc_timeout"
    sensor.default = 5
    sensor.min_value = 0
    sensor.max_value = 60
    sensor.step = 1
    sensor.category = Category.REMOTE_CONTROL_SETTING
    sensor.poll_speed = None

    assert SolisSensor(hass, sensor)._attr_entity_category == EntityCategory.CONFIG
    assert SolisNumberEntity(hass, sensor)._attr_entity_category == EntityCategory.CONFIG


def test_configurable_platform_entities_are_configuration_entities(hass):
    controller = _controller()
    controller.device_id = 1
    controller.host = "1.2.3.4"
    switch = SolisBinaryEntity(hass, controller, {"register": 43110, "bit_position": 0, "name": "Self-Use Mode"})
    select = SolisSelectEntity(
        hass,
        controller,
        {"register": 43110, "name": "Storage Mode", "entities": [{"name": "Self Use", "bit_position": 0}]},
    )
    time = SolisTimeEntity(hass, controller, {"register": 43143, "name": "Charge Start", "unique": "charge_start"})

    assert switch._attr_entity_category == EntityCategory.CONFIG
    assert select._attr_entity_category == EntityCategory.CONFIG
    assert time._attr_entity_category == EntityCategory.CONFIG
