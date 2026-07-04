from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solisconnect.sensors.solis_select_entity import (
    SolisSelectEntity,
    set_bit,
)


@pytest.fixture
def mock_controller():
    controller = MagicMock()
    controller.connected.return_value = True
    controller.host = "10.0.0.1"
    controller.model = "S6"
    controller.device_identification = "XYZ"
    controller.sw_version = "1.0"
    controller.async_write_holding_register = AsyncMock()
    controller.async_read_holding_register = AsyncMock()
    return controller


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.create_task = MagicMock()
    return hass


def test_set_bit_treats_none_as_zero():
    assert set_bit(None, 2, True) == 4
    assert set_bit(None, 2, False) == 0


@pytest.mark.asyncio
async def test_set_register_bit_with_uncached_register(mock_hass, mock_controller):
    """Issue #402: selecting a bit option before the first poll must read the live
    register value first instead of starting from 0 and clearing the other bits."""
    register = 43110
    live_value = set_bit(set_bit(0, 6, True), 11, True)  # bits 6 and 11 already set on the device
    mock_controller.async_read_holding_register.return_value = [live_value]

    with (
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_get", return_value=None),
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_save"),
    ):
        entity = SolisSelectEntity(mock_hass, mock_controller, {"register": register, "name": "Work Mode", "entities": []})
        await entity.set_register_bit(None, bit_position=0, conflicts_with=None, requires=None)

        mock_controller.async_read_holding_register.assert_awaited_once_with(register, 1)
        expected = set_bit(live_value, 0, True)  # other bits preserved
        mock_controller.async_write_holding_register.assert_awaited_once_with(register, expected)


@pytest.mark.asyncio
async def test_set_register_bit_uncached_failed_read_skips_write(mock_hass, mock_controller):
    """Issue #402: with an empty cache and a failed live read, nothing must be written."""
    register = 43110
    mock_controller.async_read_holding_register.return_value = None

    with (
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_get", return_value=None),
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_save"),
    ):
        entity = SolisSelectEntity(mock_hass, mock_controller, {"register": register, "name": "Work Mode", "entities": []})
        await entity.set_register_bit(None, bit_position=0, conflicts_with=None, requires=None)

        mock_controller.async_write_holding_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_register_bit_enforces_conflicts_and_requires(mock_hass, mock_controller):
    register = 43110
    entity_def = {
        "register": register,
        "name": "Work Mode",
        "entities": [
            {"bit_position": 0, "name": "Self-Use", "conflicts_with": (6, 11)},
            {"bit_position": 1, "name": "Self-Use + TOU", "requires": (0,)},
        ],
    }

    # 6 and 11 are on
    with (
        patch(
            "custom_components.solisconnect.sensors.solis_select_entity.cache_get",
            return_value=set_bit(set_bit(0, 6, True), 11, True),
        ),
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_save"),
    ):
        entity = SolisSelectEntity(mock_hass, mock_controller, entity_def)
        await entity.set_register_bit(None, bit_position=0, conflicts_with=(6, 11), requires=None)

        expected = set_bit(set_bit(set_bit(0, 6, False), 11, False), 0, True)
        mock_controller.async_write_holding_register.assert_awaited_once_with(register, expected)


@pytest.mark.asyncio
async def test_set_register_bit_writes_when_only_conflict_bits_change(mock_hass, mock_controller):
    """If the target bit is already set but a conflicting bit must be cleared,
    the write must still happen (regression: comparison against a mutated
    working copy used to skip it)."""
    register = 43110
    cached = set_bit(set_bit(0, 0, True), 6, True)  # target bit 0 already on, conflict bit 6 on

    with (
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_get", return_value=cached),
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_save"),
    ):
        entity = SolisSelectEntity(mock_hass, mock_controller, {"register": register, "name": "Work Mode", "entities": []})
        await entity.set_register_bit(None, bit_position=0, conflicts_with=(6,), requires=None)

        expected = set_bit(0, 0, True)  # conflict cleared, target kept
        mock_controller.async_write_holding_register.assert_awaited_once_with(register, expected)


@pytest.mark.asyncio
async def test_set_register_bit_with_requires(mock_hass, mock_controller):
    register = 43110
    with (
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_get", return_value=0),
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_save"),
    ):
        entity = SolisSelectEntity(mock_hass, mock_controller, {"register": register, "name": "Work Mode", "entities": []})

        await entity.set_register_bit(None, bit_position=1, conflicts_with=None, requires=[0])

        expected = set_bit(set_bit(0, 0, True), 1, True)
        mock_controller.async_write_holding_register.assert_awaited_once_with(register, expected)


@pytest.mark.asyncio
async def test_companion_writes_after_on_value_select(mock_hass, mock_controller):
    """After writing the primary register, companion registers must be re-written from cache, in order.

    Solis firmware requires Forced Charge/Discharge (43135) to be enabled BEFORE the
    related setpoints (43129, 43136) and RC Timeout (43282) are written for the values
    to latch (issue #352).
    """
    primary_register = 43135
    cached = {
        43136: 1500,
        43129: 1200,
        43282: 30,
    }

    def fake_cache_get(_hass, _controller, register):
        return cached.get(register)

    with patch(
        "custom_components.solisconnect.sensors.solis_select_entity.cache_get",
        side_effect=fake_cache_get,
    ):
        entity = SolisSelectEntity(
            mock_hass,
            mock_controller,
            {
                "register": primary_register,
                "name": "RC Force Charge/Discharge",
                "companion_writes": [43136, 43129, 43282],
                "entities": [
                    {"name": "None", "on_value": 0},
                    {"name": "Solis RC Force Battery Charge", "on_value": 1},
                ],
            },
        )
        entity.async_write_ha_state = MagicMock()

        await entity.async_select_option("Solis RC Force Battery Charge")

        assert mock_controller.async_write_holding_register.await_args_list == [
            ((primary_register, 1), {}),
            ((43136, cached[43136]), {}),
            ((43129, cached[43129]), {}),
            ((43282, cached[43282]), {}),
        ]


@pytest.mark.asyncio
async def test_companion_writes_skip_individual_uncached_registers(mock_hass, mock_controller):
    """Companion writes only fire for registers that already have a cached value."""
    primary_register = 43135
    cached = {
        43136: 1500,
        # 43129 not yet polled
        43282: 30,
    }

    def fake_cache_get(_hass, _controller, register):
        return cached.get(register)

    with patch(
        "custom_components.solisconnect.sensors.solis_select_entity.cache_get",
        side_effect=fake_cache_get,
    ):
        entity = SolisSelectEntity(
            mock_hass,
            mock_controller,
            {
                "register": primary_register,
                "name": "RC Force Charge/Discharge",
                "companion_writes": [43136, 43129, 43282],
                "entities": [
                    {"name": "None", "on_value": 0},
                    {"name": "Solis RC Force Battery Charge", "on_value": 1},
                ],
            },
        )
        entity.async_write_ha_state = MagicMock()

        await entity.async_select_option("Solis RC Force Battery Charge")

        assert mock_controller.async_write_holding_register.await_args_list == [
            ((primary_register, 1), {}),
            ((43136, cached[43136]), {}),
            ((43282, cached[43282]), {}),
        ]


@pytest.mark.asyncio
async def test_companion_writes_skipped_when_cache_empty(mock_hass, mock_controller):
    """Companion writes should be skipped if the cached value isn't available yet."""
    primary_register = 43135
    companion_register = 43282

    with patch(
        "custom_components.solisconnect.sensors.solis_select_entity.cache_get",
        return_value=None,
    ):
        entity = SolisSelectEntity(
            mock_hass,
            mock_controller,
            {
                "register": primary_register,
                "name": "RC Force Charge/Discharge",
                "companion_writes": [companion_register],
                "entities": [
                    {"name": "None", "on_value": 0},
                    {"name": "Solis RC Force Battery Charge", "on_value": 1},
                ],
            },
        )
        entity.async_write_ha_state = MagicMock()

        await entity.async_select_option("Solis RC Force Battery Charge")

        mock_controller.async_write_holding_register.assert_awaited_once_with(primary_register, 1)


def _work_mode_definition():
    from custom_components.solisconnect.data.solis_config import SOLIS_INVERTERS, InverterOptions
    from custom_components.solisconnect.sensor_data.select_sensors import get_select_sensors

    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(), "S2_WL_ST")
    return next(group for group in get_select_sensors(config) if group["name"] == "Work Mode")


# The 8 field-tested whole-register values copied from wills106/homeassistant-solax-modbus
# (plugin_solis_fb00.py). None includes bit 1 (Timed Charge / TOU), which does not persist
# on this firmware family.
_WORK_MODE_VALUES = {
    "Self-Use - No Grid Charging": 1,
    "Backup/Reserve - No Grid Charging": 17,
    "Self-Use": 33,
    "Off-Grid Mode": 37,
    "Battery Awaken": 41,
    "Backup/Reserve": 49,
    "Feed-in priority - No Grid Charging": 64,
    "Feed-in priority": 96,
}


def test_work_mode_definition_matches_solax_values():
    """The Work Mode options must be the exact solax_modbus-validated whole-register values,
    with no bit-conflict logic and no bit-1 (Timed Charge) option."""
    definition = _work_mode_definition()
    by_name = {e["name"]: e for e in definition["entities"]}

    assert set(by_name) == set(_WORK_MODE_VALUES)
    for name, expected in _WORK_MODE_VALUES.items():
        assert by_name[name]["on_value"] == expected
        assert "bit_position" not in by_name[name]
        assert "conflicts_with" not in by_name[name]
        # No option sets bit 1 (Timed Charge) - it doesn't persist on this firmware.
        assert (expected >> 1) & 1 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "value"), list(_WORK_MODE_VALUES.items()))
async def test_work_mode_option_writes_whole_register_value(mock_hass, mock_controller, name, value):
    """Selecting a Work Mode option writes its whole-register value directly (no bit math)."""
    definition = _work_mode_definition()

    with (
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_get", return_value=0),
        patch("custom_components.solisconnect.sensors.solis_select_entity.cache_save"),
    ):
        entity = SolisSelectEntity(mock_hass, mock_controller, definition)
        entity.async_write_ha_state = MagicMock()
        await entity.async_select_option(name)

        mock_controller.async_write_holding_register.assert_awaited_once_with(43110, value)


@pytest.mark.parametrize(("name", "value"), list(_WORK_MODE_VALUES.items()))
def test_work_mode_current_option_resolves_each_value(mock_hass, mock_controller, name, value):
    """current_option must map each whole-register value back to its option name."""
    definition = _work_mode_definition()

    with patch("custom_components.solisconnect.sensors.solis_select_entity.cache_get", return_value=value):
        entity = SolisSelectEntity(mock_hass, mock_controller, definition)
        assert entity.current_option == name
