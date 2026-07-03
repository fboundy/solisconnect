from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solisconnect.const import (
    CONF_CLOUD_KEY_ID,
    CONF_CLOUD_KEY_SECRET,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_USERNAME,
    CONTROLLER,
    DOMAIN,
)
from custom_components.solisconnect.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_credentials(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SN1",
        data={
            CONF_CLOUD_KEY_ID: "kid",
            CONF_CLOUD_KEY_SECRET: "secret",
            CONF_CLOUD_USERNAME: "user@example.com",
            CONF_CLOUD_PASSWORD: "pw",
            "model": "S6-EH1P",
        },
    )
    entry.add_to_hass(hass)

    controller = MagicMock()
    controller.model = "S6-EH1P"
    controller.serial_number = "SN1"
    controller.cache_scope = "SN1"
    controller.connected.return_value = True
    hass.data.setdefault(DOMAIN, {}).setdefault(CONTROLLER, {})[entry.entry_id] = controller

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    data = diagnostics["entry"]["data"]
    assert data[CONF_CLOUD_KEY_ID] == "***"
    assert data[CONF_CLOUD_KEY_SECRET] == "***"
    assert data[CONF_CLOUD_USERNAME] == "***"
    assert data[CONF_CLOUD_PASSWORD] == "***"
    assert diagnostics["controller"]["connected"] is True
    assert diagnostics["cloud_mapping"]["control_cids"] > 0
