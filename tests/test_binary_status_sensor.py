from unittest.mock import MagicMock

import pytest

from custom_components.solisconnect.const import REGISTER, SCOPE, VALUE
from custom_components.solisconnect.helpers import controller_scope
from custom_components.solisconnect.sensor_data.binary_sensors import get_binary_sensors
from custom_components.solisconnect.sensors.solis_binary_status_sensor import SolisBinaryStatusSensor


@pytest.fixture
def controller():
    mock = MagicMock()
    mock.host = "inverter.local"
    mock.device_id = 1
    mock.identification = None
    mock.device_serial_number = "SN123"
    mock.serial_number = "SN123"
    mock.model = "S6"
    mock.device_identification = "XYZ"
    mock.sw_version = "1.0"
    return mock


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    return hass


def _update(controller, register, value):
    return {REGISTER: register, VALUE: value, SCOPE: controller_scope(controller)}


def test_binary_status_sensor_has_no_write_methods(mock_hass, controller):
    entity = SolisBinaryStatusSensor(mock_hass, controller, {"register": 43110, "bit_position": 1, "name": "Timed Charge Status"})
    assert not hasattr(entity, "async_turn_on")
    assert not hasattr(entity, "async_turn_off")
    assert not hasattr(entity, "set_register_bit")


def test_binary_status_sensor_bit_update_sets_is_on(mock_hass, controller):
    entity = SolisBinaryStatusSensor(mock_hass, controller, {"register": 43110, "bit_position": 1, "name": "Timed Charge Status"})
    entity.schedule_update_ha_state = MagicMock()

    entity.handle_modbus_update(_update(controller, 43110, 0b11))  # bit 1 set
    assert entity.is_on is True
    assert entity._attr_available is True
    entity.schedule_update_ha_state.assert_called_once()

    entity.handle_modbus_update(_update(controller, 43110, 0b01))  # bit 1 clear
    assert entity.is_on is False


def test_binary_status_sensor_ignores_other_register(mock_hass, controller):
    entity = SolisBinaryStatusSensor(mock_hass, controller, {"register": 43110, "bit_position": 1, "name": "Timed Charge Status"})
    entity.schedule_update_ha_state = MagicMock()

    entity.handle_modbus_update(_update(controller, 43111, 0b11))
    entity.schedule_update_ha_state.assert_not_called()


def test_binary_status_sensor_ignores_wrong_controller(mock_hass, controller):
    entity = SolisBinaryStatusSensor(mock_hass, controller, {"register": 43110, "bit_position": 1, "name": "Timed Charge Status"})
    entity.schedule_update_ha_state = MagicMock()

    entity.handle_modbus_update({REGISTER: 43110, VALUE: 0b11, SCOPE: "some-other-inverter"})
    entity.schedule_update_ha_state.assert_not_called()


def test_binary_status_sensor_device_info_delegates(mock_hass, controller):
    entity = SolisBinaryStatusSensor(mock_hass, controller, {"register": 43110, "bit_position": 1, "name": "Timed Charge Status"})
    assert entity.device_info is controller.device_info


def test_get_binary_sensors_exposes_timed_charge_status():
    from custom_components.solisconnect.data.solis_config import SOLIS_INVERTERS, InverterOptions

    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(), "S2_WL_ST")

    groups = get_binary_sensors(config)
    entities = [e for group in groups for e in group["entities"]]
    names = {e["name"] for e in entities}

    assert "Timed Charge Status" in names
    tou = next(e for group in groups for e in group["entities"] if e["name"] == "Timed Charge Status")
    assert group_register(groups, "Timed Charge Status") == 43110
    assert tou["bit_position"] == 1


def group_register(groups, name):
    for group in groups:
        if any(e["name"] == name for e in group["entities"]):
            return group["register"]
    return None
