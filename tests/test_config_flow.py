from unittest.mock import patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solisconnect.const import CONN_TYPE_TCP, DOMAIN


def _encode_serial_registers(serial: str) -> list[int]:
    """Inverse of helpers.extract_serial_number: pack an ASCII serial into 16-bit words."""
    padded = serial if len(serial) % 2 == 0 else serial + "\x00"
    return [(ord(padded[i]) << 8) | ord(padded[i + 1]) for i in range(0, len(padded), 2)]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.mark.asyncio
async def test_flow_user_success(hass: HomeAssistant):
    """Test user initialized flow with success."""
    with (
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.connect",
            return_value=True,
        ) as mock_connect,
        patch(
            "custom_components.solisconnect.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        # Step 1: Select connection type
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        # Step 2: Configure connection details
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "config"

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_v2": True,
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            "inverter_serial": "sn123",  # Lowercase input
        }

        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

        # CHANGED: The title is now based on the serial number
        assert result["title"] == "Solis: SN123"

        # Verify data
        assert result["data"]["connection_type"] == CONN_TYPE_TCP

        # CHANGED: Verify Serial was converted to UPPERCASE
        assert result["data"]["inverter_serial"] == "SN123"

        # Verify other fields
        assert result["data"]["host"] == "1.2.3.4"
        assert result["data"]["slave"] == 1

        mock_connect.assert_called()
        mock_setup_entry.assert_called_once()


@pytest.mark.asyncio
async def test_flow_user_hmi_version_overrides_has_v2(hass: HomeAssistant):
    """Issue #20: a determinate HMI version (register 33002) overrides the form's has_v2 choice."""

    async def fake_read_input_register(register, count):
        if register == 33002:
            return [0x4AFF]  # below the V2 threshold
        if register == 33004:
            return None  # no serial data available; must not corrupt the typed serial
        return [1]

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            side_effect=fake_read_input_register,
        ),
        patch("custom_components.solisconnect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_v2": True,  # user picked True; the detected HMI version should override it
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            "inverter_serial": "sn124",
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"]["has_v2"] is False
        assert result["data"]["inverter_serial"] == "SN124"  # no serial detected; typed value survives


@pytest.mark.asyncio
async def test_flow_user_hmi_read_failure_keeps_user_choice(hass: HomeAssistant):
    """Issue #20: an HMI version read failure must never block setup or override the user's choice."""

    async def fake_read_input_register(register, count):
        if register == 33002:
            raise TimeoutError("no response")
        if register == 33004:
            return None
        return [1]

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            side_effect=fake_read_input_register,
        ),
        patch("custom_components.solisconnect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_v2": False,
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            "inverter_serial": "sn125",
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"]["has_v2"] is False
        assert result["data"]["inverter_serial"] == "SN125"


@pytest.mark.asyncio
async def test_flow_user_device_serial_overrides_typed_serial(hass: HomeAssistant):
    """A serial read from device registers 33004-33019 overrides a mistyped form value."""
    device_serial_registers = _encode_serial_registers("1234567890123456")

    async def fake_read_input_register(register, count):
        if register == 33004:
            return device_serial_registers
        return [1]

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            side_effect=fake_read_input_register,
        ),
        patch("custom_components.solisconnect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_v2": True,
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            "inverter_serial": "wrongserial",
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"]["inverter_serial"] == "1234567890123456"


@pytest.mark.asyncio
async def test_flow_user_grid_model_skips_serial_detection(hass: HomeAssistant):
    """Grid/string inverters don't expose the serial-number register range; detection must be skipped."""
    registers_read = []

    async def fake_read_input_register(register, count):
        registers_read.append(register)
        return [1]

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            side_effect=fake_read_input_register,
        ),
        patch("custom_components.solisconnect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-GR1P",
            "connection": "S2_WL_ST",
            "has_v2": True,
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            "inverter_serial": "sn126",
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"]["inverter_serial"] == "SN126"
        assert 33004 not in registers_read  # serial detection is hybrid-only; grid/string lacks this register range
        assert 33002 in registers_read  # HMI-version detection is not gated by inverter type


@pytest.mark.asyncio
async def test_flow_user_blank_serial_auto_detected_from_device(hass: HomeAssistant):
    """The serial field is optional: a hybrid model can be set up without typing it at all."""
    device_serial_registers = _encode_serial_registers("1234567890123456")

    async def fake_read_input_register(register, count):
        if register == 33004:
            return device_serial_registers
        return [1]

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            side_effect=fake_read_input_register,
        ),
        patch("custom_components.solisconnect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_v2": True,
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            # inverter_serial omitted entirely
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"]["inverter_serial"] == "1234567890123456"


@pytest.mark.asyncio
async def test_flow_user_blank_serial_without_detection_shows_error(hass: HomeAssistant):
    """A grid model with no typed serial has no way to auto-detect one; the form must ask again."""

    async def fake_read_input_register(register, count):
        if register == 33004:
            return None  # grid models don't expose this range anyway; belt-and-braces
        return [1]

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            side_effect=fake_read_input_register,
        ),
        patch("custom_components.solisconnect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-GR1P",
            "connection": "S2_WL_ST",
            "has_v2": True,
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            # inverter_serial omitted; grid model can't auto-detect it
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "config"
        assert "manually" in result["errors"]["base"]


@pytest.mark.asyncio
async def test_flow_user_connection_error(hass: HomeAssistant):
    """Test user initialized flow with connection error."""
    with patch(
        "custom_components.solisconnect.modbus_controller.ModbusController.connect",
        return_value=False,
    ):
        # Step 1: Select connection type
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        # Step 2: Configure connection details
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "config"

        config_input = {
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_v2": True,
            "has_pv": True,
            "has_ac_coupling": False,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": False,
            "inverter_serial": "sn123",
        }

        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["errors"] == {"base": "Cannot connect to Modbus device. Please check your configuration."}


@pytest.mark.asyncio
async def test_flow_user_duplicates(hass: HomeAssistant):
    """Test user initialized flow with duplicate entry."""

    # CHANGED: Setup existing entry with SERIAL NUMBER as unique_id
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SN123",  # Matches uppercase serial
        data={"host": "1.2.3.4", "slave": 1, "connection_type": CONN_TYPE_TCP, "inverter_serial": "SN123"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.solisconnect.modbus_controller.ModbusController.connect",
        return_value=True,
    ):
        # Step 1: Select connection type
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"connection_type": CONN_TYPE_TCP})

        # Step 2: Configure connection details (duplicate config)
        config_input = {
            "host": "1.2.3.4",  # Even if host is same
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_generator": False,
            "inverter_serial": "sn123",  # Try adding same serial (lowercase)
        }

        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=config_input)

        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_options_flow_suggestions_use_merged_data_and_options(hass: HomeAssistant):
    """Feature toggles live in entry.data until options are saved; the form must pre-fill from data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SNOPT1",
        version=3,
        data={
            "connection_type": CONN_TYPE_TCP,
            "inverter_serial": "SNOPT1",
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "model": "S6-EH1P",
            "connection": "S2_WL_ST",
            "has_v2": True,
            "has_pv": True,
            "has_ac_coupling": True,
            "has_parallel": False,
            "has_battery": True,
            "has_hv_battery": False,
            "has_generator": True,
            "poll_interval_fast": 10,
            "poll_interval_normal": 15,
            "poll_interval_slow": 30,
        },
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"

    schema = result["data_schema"]
    ac_key = next(k for k in schema.schema if getattr(k, "schema", None) == "has_ac_coupling")
    gen_key = next(k for k in schema.schema if getattr(k, "schema", None) == "has_generator")
    assert (ac_key.description or {}).get("suggested_value") is True
    assert (gen_key.description or {}).get("suggested_value") is True

    user_input = {
        "poll_interval_fast": 10,
        "poll_interval_normal": 15,
        "poll_interval_slow": 30,
        "model": "S6-EH1P",
        "connection": "S2_WL_ST",
        "has_v2": True,
        "has_pv": True,
        "has_ac_coupling": True,
        "has_parallel": False,
        "has_battery": True,
        "has_hv_battery": False,
        "has_generator": True,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=user_input)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options["has_ac_coupling"] is True
    assert entry.options["has_generator"] is True
