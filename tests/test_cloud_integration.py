"""End-to-end tests for the cloud-only path: setup, retrieval into the shared cache,
entity updates through the UNCHANGED entity classes, migration, and the config flow branch."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solisconnect.const import (
    CONF_CLOUD_DETECTED_MODEL,
    CONF_CLOUD_INVERTER_ID,
    CONF_CLOUD_KEY_ID,
    CONF_CLOUD_KEY_SECRET,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PLANT_ID,
    CONF_CLOUD_PRODUCT_MODEL,
    CONF_CLOUD_USERNAME,
    CONF_CONNECTION_TYPE,
    CONF_INVERTER_SERIAL,
    CONF_PROTOCOL_MODE,
    CONN_TYPE_CLOUD,
    CONTROLLER,
    DOMAIN,
    PROTO_CLOUD_ONLY,
    PROTO_MODBUS_ONLY,
    SENSOR_ENTITIES,
    VALUES,
)

SERIAL = "CLOUDSN1"

CANNED_DETAIL = {
    "pac": 0.027,
    "pacStr": "kW",
    "eToday": 22.8,
    "fac": 50.12,
    "inverterTemperature": 45.4,
    "currentState": 3,
    "batteryCapacitySoc": 59.0,
    "batteryPower": -1.596,
    "batteryPowerStr": "kW",
    "familyLoadPower": 0.915,
    "familyLoadPowerStr": "kW",
}

CANNED_BATCH = {
    636: "33",
    157: "52",
    158: "15",
    # All 12 TOU V2 switch-slot CIDs must be present for the merged 43707 bitfield to publish
    # on a cold cache (see CloudDataRetrieval._merge_switch_bits): a partial batch is treated
    # as "unknown so far" rather than defaulting the unseen bits to off.
    5916: "1",
    5917: "0",
    5918: "0",
    5919: "0",
    5920: "0",
    5921: "0",
    5922: "0",
    5923: "0",
    5924: "0",
    5925: "0",
    5926: "0",
    5927: "1",
    5946: "09:30-10:45",
    5948: "12.5",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def mock_clientsession():
    """The real session's DNS resolver can't construct on Windows test loops, and the
    API methods are mocked in these tests anyway."""
    with patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=MagicMock()):
        yield


def _cloud_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        version=4,
        data={
            CONF_PROTOCOL_MODE: PROTO_CLOUD_ONLY,
            CONF_CONNECTION_TYPE: CONN_TYPE_CLOUD,
            CONF_INVERTER_SERIAL: SERIAL,
            CONF_CLOUD_KEY_ID: "kid",
            CONF_CLOUD_KEY_SECRET: "ksecret",
            CONF_CLOUD_PLANT_ID: "plant1",
            CONF_CLOUD_USERNAME: "user@example.com",
            CONF_CLOUD_PASSWORD: "pw",
            "model": "S6-EH1P",
        },
        title="Solis Cloud Inverter",
    )


def _patched_api():
    return (
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_detail",
            new=AsyncMock(return_value=CANNED_DETAIL),
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_at_read_batch",
            new=AsyncMock(return_value=CANNED_BATCH),
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_list",
            new=AsyncMock(return_value=[{"id": "CLOUDID1", "sn": SERIAL}]),
        ),
    )


async def test_cloud_only_setup_populates_cache_with_raw_words(hass: HomeAssistant):
    entry = _cloud_entry()
    entry.add_to_hass(hass)

    detail_patch, batch_patch, list_patch = _patched_api()
    with detail_patch, batch_patch, list_patch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state.value == "loaded"
        hub = hass.data[DOMAIN][CONTROLLER][entry.entry_id]
        assert hub.cloud is not None and hub.modbus is None
        assert hub.active is hub.cloud
        assert hub.cache_scope == SERIAL

        # The entry has no stored cloud id: the retrieval must self-heal via inverterList
        # and pass the resolved id to inverterDetail (sn alone returns no usable fields live).
        assert hub.cloud.inverter_id == "CLOUDID1"
        detail_mock = hub.cloud.api.async_inverter_detail
        assert detail_mock.await_args.args == (SERIAL, "CLOUDID1")

        values = hass.data[DOMAIN][VALUES]
        # Cloud engineering values inverse-converted to the exact raw words Modbus would return
        assert values[f"{SERIAL}|33035"] == 228  # eToday 22.8 kWh @ x0.1
        assert values[f"{SERIAL}|33079"] == 0 and values[f"{SERIAL}|33080"] == 27  # pac 0.027 kW -> 27 W S32
        assert values[f"{SERIAL}|33094"] == 5012  # fac 50.12 Hz @ x0.01
        assert values[f"{SERIAL}|33093"] == 454  # temperature 45.4 @ x0.1
        assert values[f"{SERIAL}|33095"] == 3  # currentState
        assert values[f"{SERIAL}|33139"] == 59  # battery SOC
        assert values[f"{SERIAL}|33135"] == 0  # battery direction: charging
        assert (values[f"{SERIAL}|33149"] << 16 | values[f"{SERIAL}|33150"]) == 1596  # |batteryPower| W
        assert values[f"{SERIAL}|33147"] == 915  # familyLoadPower W
        assert values[f"{SERIAL}|43110"] == 33  # cid 636
        assert values[f"{SERIAL}|43024"] == 52  # cid 157
        assert values[f"{SERIAL}|43011"] == 15  # cid 158
        assert values[f"{SERIAL}|43707"] == 2049  # cid 5916 bit 0 + cid 5927 bit 11
        assert values[f"{SERIAL}|43711"] == 9
        assert values[f"{SERIAL}|43712"] == 30
        assert values[f"{SERIAL}|43713"] == 10
        assert values[f"{SERIAL}|43714"] == 45
        assert values[f"{SERIAL}|43709"] == 125  # cid 5948 = 12.5 A, register multiplier x0.1

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_cloud_only_entities_update_through_unchanged_classes(hass: HomeAssistant):
    entry = _cloud_entry()
    entry.add_to_hass(hass)

    detail_patch, batch_patch, list_patch = _patched_api()
    with detail_patch, batch_patch, list_patch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        sensors = {e.base_sensor.name: e for e in hass.data[DOMAIN][SENSOR_ENTITIES] if e.base_sensor.controller.serial_number == SERIAL}

        soc = sensors["Battery SOC"]
        assert soc._attr_native_value == 59
        # Same serial-scoped unique_id scheme a Modbus entry would produce
        assert soc._attr_unique_id.startswith(f"{DOMAIN}_{SERIAL}_")

        today = sensors["PV Today Energy Generation"]
        assert today._attr_native_value == pytest.approx(22.8)

        # A register with no cloud mapping must be unavailable, not stale
        unmapped = sensors["PV Bus Voltage"]  # register 33071, unmapped
        assert unmapped.base_sensor.enabled is False
        assert unmapped._attr_available is False

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_cloud_write_unmapped_register_raises(hass: HomeAssistant):
    entry = _cloud_entry()
    entry.add_to_hass(hass)

    detail_patch, batch_patch, list_patch = _patched_api()
    with detail_patch, batch_patch, list_patch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        from homeassistant.exceptions import HomeAssistantError

        hub = hass.data[DOMAIN][CONTROLLER][entry.entry_id]
        # RTC block has no cloud write mapping (deliberately)
        with pytest.raises(HomeAssistantError, match="SolisCloud"):
            await hub.async_write_holding_register(43000, 26)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_migration_v3_to_v4_sets_modbus_only(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SN123456",
        version=3,
        data={
            "host": "1.2.3.4",
            "port": 502,
            "slave": 1,
            "inverter_serial": "SN123456",
            "model": "S6-EH1P",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connected", return_value=True),
        patch("custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register", return_value=[1, 2, 3]),
        patch("custom_components.solisconnect.modbus_controller.ModbusController.async_read_holding_register", return_value=[1, 2, 3]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.version == 4
        assert entry.data[CONF_PROTOCOL_MODE] == PROTO_MODBUS_ONLY
        assert entry.unique_id == "SN123456"  # no unique_id churn

        hub = hass.data[DOMAIN][CONTROLLER][entry.entry_id]
        assert hub.modbus is not None and hub.cloud is None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_config_flow_cloud_branch_creates_entry(hass: HomeAssistant):
    with (
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_list",
            new=AsyncMock(return_value=[{"id": "1", "sn": "sn999", "productModel": "3102"}]),
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_detail",
            new=AsyncMock(return_value={"hmiVersionAll": "4B00"}),
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] == "form" and result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CONNECTION_TYPE: CONN_TYPE_CLOUD})
        assert result["type"] == "form" and result["step_id"] == "cloud"

        with patch("custom_components.solisconnect.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    CONF_CLOUD_KEY_ID: "kid",
                    CONF_CLOUD_KEY_SECRET: "ksecret",
                    CONF_CLOUD_PLANT_ID: "plant1",
                    "model": "S6-EH1P",
                },
            )

        assert result["type"] == "create_entry"
        assert result["data"][CONF_INVERTER_SERIAL] == "SN999"  # auto-detected, uppercased
        assert result["data"][CONF_CLOUD_INVERTER_ID] == "1"  # cloud id stored for inverterDetail
        assert result["data"][CONF_CLOUD_PRODUCT_MODEL] == "3102"
        assert result["data"][CONF_CLOUD_DETECTED_MODEL] == "S5-EH1P"
        assert result["data"]["model"] == "S5-EH1P"
        assert result["data"][CONF_PROTOCOL_MODE] == PROTO_CLOUD_ONLY
        assert result["title"] == "Solis: SN999 (Cloud)"
        assert result["data"]["has_v2"] is True  # hmiVersionAll "4B00" meets the V2 threshold


async def test_config_flow_cloud_bad_credentials_shows_error(hass: HomeAssistant):
    from custom_components.solisconnect.cloud.api_client import SolisCloudApiError

    with patch(
        "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_list",
        new=AsyncMock(side_effect=SolisCloudApiError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CONNECTION_TYPE: CONN_TYPE_CLOUD})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CLOUD_KEY_ID: "kid", CONF_CLOUD_KEY_SECRET: "bad", CONF_CLOUD_PLANT_ID: "plant1", "model": "S6-EH1P"},
        )

        assert result["type"] == "form"
        assert result["errors"]["base"]


async def test_reconfigure_cloud_entry_uses_cloud_form(hass: HomeAssistant):
    """Reconfigure on a cloud entry must not attempt a Modbus probe."""
    entry = _cloud_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_list",
            new=AsyncMock(return_value=[{"id": "CLOUDID1", "sn": SERIAL}]),
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_detail",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == "form"
        assert result["step_id"] == "reconfigure_cloud"

        with (
            patch("custom_components.solisconnect.async_setup_entry", return_value=True),
            patch("custom_components.solisconnect.async_unload_entry", return_value=True),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    CONF_CLOUD_KEY_ID: "kid2",
                    CONF_CLOUD_KEY_SECRET: "ksecret2",
                    CONF_CLOUD_PLANT_ID: "plant1",
                    CONF_INVERTER_SERIAL: SERIAL,
                    "model": "S6-EH1P",
                },
            )
        assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_CLOUD_KEY_ID] == "kid2"
        assert entry.data[CONF_CLOUD_INVERTER_ID] == "CLOUDID1"


async def test_reconfigure_cloud_entry_hmi_version_overrides_has_v2(hass: HomeAssistant):
    """Issue #20: reconfiguring a cloud-only entry also picks up HMI-based V2 detection."""
    entry = _cloud_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_list",
            new=AsyncMock(return_value=[{"id": "CLOUDID1", "sn": SERIAL}]),
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_detail",
            new=AsyncMock(return_value={"hmiVersionAll": "4B00"}),
        ),
    ):
        result = await entry.start_reconfigure_flow(hass)

        with (
            patch("custom_components.solisconnect.async_setup_entry", return_value=True),
            patch("custom_components.solisconnect.async_unload_entry", return_value=True),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    CONF_CLOUD_KEY_ID: "kid2",
                    CONF_CLOUD_KEY_SECRET: "ksecret2",
                    CONF_CLOUD_PLANT_ID: "plant1",
                    CONF_INVERTER_SERIAL: SERIAL,
                    "model": "S6-EH1P",
                    "has_v2": False,  # form choice; the detected "4B00" should override it
                },
            )
        assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
        assert entry.data["has_v2"] is True


async def test_config_flow_cloud_multiple_inverters_requires_serial(hass: HomeAssistant):
    with (
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_list",
            new=AsyncMock(return_value=[{"id": "1", "sn": "SNA"}, {"id": "2", "sn": "SNB"}]),
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_detail",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CONNECTION_TYPE: CONN_TYPE_CLOUD})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CLOUD_KEY_ID: "kid", CONF_CLOUD_KEY_SECRET: "ks", CONF_CLOUD_PLANT_ID: "plant1", "model": "S6-EH1P"},
        )
        assert result["type"] == "form"
        assert "multiple inverters" in result["errors"]["base"]

        # Providing the serial resolves it
        with patch("custom_components.solisconnect.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    CONF_CLOUD_KEY_ID: "kid",
                    CONF_CLOUD_KEY_SECRET: "ks",
                    CONF_CLOUD_PLANT_ID: "plant1",
                    CONF_INVERTER_SERIAL: "snb",
                    "model": "S6-EH1P",
                },
            )
        assert result["type"] == "create_entry"
        assert result["data"][CONF_INVERTER_SERIAL] == "SNB"


async def test_dual_flow_cloud_model_overrides_wrong_modbus_model(hass: HomeAssistant):
    from custom_components.solisconnect.config_flow import CONN_TYPE_BOTH
    from custom_components.solisconnect.const import CONN_TYPE_TCP, PROTO_BOTH_FAILOVER

    async def fake_read_input_register(register, count):
        if register == 33004:
            return None  # no serial data; the Modbus serial stays "sn999" as typed
        return [1]

    with (
        patch("custom_components.solisconnect.modbus_controller.ModbusController.connect", return_value=True),
        patch(
            "custom_components.solisconnect.modbus_controller.ModbusController.async_read_input_register",
            side_effect=fake_read_input_register,
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_list",
            new=AsyncMock(return_value=[{"id": "1", "sn": "sn999", "productModel": "3102"}]),
        ),
        patch(
            "custom_components.solisconnect.cloud.api_client.SolisCloudApiClient.async_inverter_detail",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CONNECTION_TYPE: CONN_TYPE_BOTH})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CONNECTION_TYPE: CONN_TYPE_TCP})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "host": "1.2.3.4",
                "port": 502,
                "slave": 1,
                "inverter_serial": "sn999",
                "model": "S5-GR1P",
                "connection": "S2_WL_ST",
                "has_v2": True,
                "has_pv": True,
                "has_ac_coupling": False,
                "has_parallel": False,
                "has_battery": True,
                "has_hv_battery": False,
                "has_generator": True,
            },
        )
        assert result["type"] == "form" and result["step_id"] == "cloud"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CLOUD_KEY_ID: "kid", CONF_CLOUD_KEY_SECRET: "ks", CONF_CLOUD_PLANT_ID: "plant1"},
        )
        assert result["type"] == "form" and result["step_id"] == "dual_mode"
        with patch("custom_components.solisconnect.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"dual_mode": "failover_modbus"})

        assert result["type"] == "create_entry"
        assert result["data"]["model"] == "S5-EH1P"
        assert result["data"][CONF_CLOUD_DETECTED_MODEL] == "S5-EH1P"
        assert result["data"][CONF_PROTOCOL_MODE] == PROTO_BOTH_FAILOVER
        # HMI read (register 33002) returned [1] via the blanket async_read_input_register
        # patch, well below the V2 threshold -> overrides the user's has_v2=True to False.
        assert result["data"]["has_v2"] is False
