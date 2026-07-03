import asyncio
import logging
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import OptionsFlow

from . import ModbusController
from .const import (
    CONF_BAUDRATE,
    CONF_BYTESIZE,
    CONF_CLOUD_DETECTED_MODEL,
    CONF_CLOUD_INVERTER_ID,
    CONF_CLOUD_KEY_ID,
    CONF_CLOUD_KEY_SECRET,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PLANT_ID,
    CONF_CLOUD_POLL_INTERVAL,
    CONF_CLOUD_PRODUCT_MODEL,
    CONF_CLOUD_USERNAME,
    CONF_CONNECTION_TYPE,
    CONF_FAILOVER_PRIMARY,
    CONF_INVERTER_SERIAL,
    CONF_PARITY,
    CONF_PROTOCOL_MODE,
    CONF_SERIAL_PORT,
    CONF_STOPBITS,
    CONN_TYPE_CLOUD,
    CONN_TYPE_SERIAL,
    CONN_TYPE_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_BYTESIZE,
    DEFAULT_CLOUD_POLL_INTERVAL,
    DEFAULT_PARITY,
    DEFAULT_STOPBITS,
    DOMAIN,
    MIN_CLOUD_POLL_INTERVAL,
    PROTO_BOTH_FAILOVER,
    PROTO_BOTH_MANUAL,
    PROTO_CLOUD_ONLY,
    PROTO_MODBUS_ONLY,
    PROTOCOL_CLOUD,
    PROTOCOL_MODBUS,
)
from .data.enums import InverterType
from .data.solis_config import CONNECTION_METHOD, SOLIS_INVERTERS, InverterConfig, inverter_options_from_config
from .helpers import extract_serial_number, hmi_version_supports_v2

_LOGGER = logging.getLogger(__name__)

# Extract model names for dropdown selection
SOLIS_MODELS = {inverter.model: inverter.model for inverter in SOLIS_INVERTERS}

# Connection type options
CONN_TYPE_BOTH = "both"
CONNECTION_TYPES = {
    CONN_TYPE_TCP: "TCP (WiFi Dongle)",
    CONN_TYPE_SERIAL: "Serial (RS485)",
    CONN_TYPE_CLOUD: "SolisCloud API",
    CONN_TYPE_BOTH: "Modbus + SolisCloud (failover or manual switch)",
}

MODBUS_LINK_TYPES = {CONN_TYPE_TCP: "TCP (WiFi Dongle)", CONN_TYPE_SERIAL: "Serial (RS485)"}

DUAL_MODE_OPTIONS = {
    "failover_modbus": "Automatic failover (Modbus primary, cloud backup)",
    "failover_cloud": "Automatic failover (cloud primary, Modbus backup)",
    "manual": "Manual switch (select entity chooses the active protocol)",
}

# Parity options
PARITY_OPTIONS = {"N": "None", "E": "Even", "O": "Odd"}

# Base schema with common fields (for both TCP and Serial)
BASE_CONFIG_SCHEMA = {
    vol.Required(CONF_CONNECTION_TYPE, default=CONN_TYPE_TCP): vol.In(CONNECTION_TYPES),
    # Optional: hybrid inverters have their serial read from the device (registers
    # 33004-33019) once connectivity is confirmed, so it doesn't need to be typed. Grid/
    # string models don't expose that register range, so _create_entry_from_input (and the
    # dual-mode Modbus step) re-check after validation and ask again if it's still blank.
    vol.Optional(CONF_INVERTER_SERIAL, default=""): str,
    vol.Required("slave", default=1): int,
    vol.Optional("poll_interval_fast", default=10): vol.All(int, vol.Range(min=10)),
    vol.Optional("poll_interval_normal", default=15): vol.All(int, vol.Range(min=15)),
    vol.Optional("poll_interval_slow", default=30): vol.All(int, vol.Range(min=30)),
    vol.Required("model", default=list(SOLIS_MODELS.keys())[0]): vol.In(SOLIS_MODELS),
    # Boolean options (Yes/No toggle)
    vol.Required("has_v2", default=True): bool,
    vol.Required("has_pv", default=True): bool,
    vol.Required("has_ac_coupling", default=False): bool,
    vol.Required("has_parallel", default=False): bool,
    vol.Required("has_battery", default=True): bool,
    vol.Required("has_hv_battery", default=False): bool,
    vol.Required("has_generator", default=False): bool,
}

# Combined schema that accepts both TCP and Serial fields (all optional except connection_type)
# This allows the form to accept either type of connection
COMBINED_CONFIG_SCHEMA = {
    **BASE_CONFIG_SCHEMA,
    # TCP fields (optional)
    vol.Optional("host", default=""): str,
    vol.Optional("port", default=502): int,
    vol.Optional("connection", default=list(CONNECTION_METHOD.keys())[0]): vol.In(CONNECTION_METHOD),
    # Serial fields (optional)
    vol.Optional(CONF_SERIAL_PORT, default="/dev/ttyUSB0"): str,
    vol.Optional(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): vol.In([9600, 19200, 38400, 57600, 115200]),
    vol.Optional(CONF_BYTESIZE, default=DEFAULT_BYTESIZE): vol.In([7, 8]),
    vol.Optional(CONF_PARITY, default=DEFAULT_PARITY): vol.In(PARITY_OPTIONS),
    vol.Optional(CONF_STOPBITS, default=DEFAULT_STOPBITS): vol.In([1, 2]),
}

# TCP-specific fields (includes WiFi dongle type)
TCP_CONFIG_SCHEMA = {
    **BASE_CONFIG_SCHEMA,
    vol.Required("host", default=""): str,
    vol.Required("port", default=502): int,
    vol.Required("connection", default=list(CONNECTION_METHOD.keys())[0]): vol.In(CONNECTION_METHOD),
}

# Serial-specific fields (no WiFi dongle type needed)
SERIAL_CONFIG_SCHEMA = {
    **BASE_CONFIG_SCHEMA,
    vol.Required(CONF_SERIAL_PORT, default="/dev/ttyUSB0"): str,
    vol.Required(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): vol.In([9600, 19200, 38400, 57600, 115200]),
    vol.Required(CONF_BYTESIZE, default=DEFAULT_BYTESIZE): vol.In([7, 8]),
    vol.Required(CONF_PARITY, default=DEFAULT_PARITY): vol.In(PARITY_OPTIONS),
    vol.Required(CONF_STOPBITS, default=DEFAULT_STOPBITS): vol.In([1, 2]),
}

# SolisCloud API fields: credentials + the shared model/feature options, but no Modbus
# link parameters or per-speed poll intervals (the cloud has one interval).
CLOUD_CONFIG_SCHEMA = {
    vol.Required(CONF_CLOUD_KEY_ID): str,
    vol.Required(CONF_CLOUD_KEY_SECRET): str,
    vol.Required(CONF_CLOUD_PLANT_ID): str,
    vol.Optional(CONF_CLOUD_USERNAME, default=""): str,
    vol.Optional(CONF_CLOUD_PASSWORD, default=""): str,
    vol.Optional(CONF_INVERTER_SERIAL, default=""): str,
    vol.Optional(CONF_CLOUD_POLL_INTERVAL, default=DEFAULT_CLOUD_POLL_INTERVAL): vol.All(int, vol.Range(min=MIN_CLOUD_POLL_INTERVAL)),
    vol.Required("model", default=list(SOLIS_MODELS.keys())[0]): vol.In(SOLIS_MODELS),
    vol.Required("has_v2", default=True): bool,
    vol.Required("has_pv", default=True): bool,
    vol.Required("has_ac_coupling", default=False): bool,
    vol.Required("has_parallel", default=False): bool,
    vol.Required("has_battery", default=True): bool,
    vol.Required("has_hv_battery", default=False): bool,
    vol.Required("has_generator", default=False): bool,
}

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required("poll_interval_fast"): vol.All(int, vol.Range(min=10)),
        vol.Required("poll_interval_normal"): vol.All(int, vol.Range(min=15)),
        vol.Required("poll_interval_slow"): vol.All(int, vol.Range(min=30)),
        vol.Required("model"): vol.In(SOLIS_MODELS),
        vol.Required("connection", default=list(CONNECTION_METHOD.keys())[0]): vol.In(CONNECTION_METHOD),
        # Boolean options (Yes/No toggle)
        vol.Required("has_v2", default=True): bool,
        vol.Required("has_pv", default=True): bool,
        vol.Required("has_ac_coupling", default=False): bool,
        vol.Required("has_parallel", default=False): bool,
        vol.Required("has_battery", default=True): bool,
        vol.Required("has_hv_battery", default=False): bool,
        vol.Required("has_generator", default=False): bool,
    }
)


def clean_identification(iden: str | None) -> str | None:
    if not iden or not iden.strip():
        return None
    # Replace spaces and disallowed characters with underscores
    iden = iden.strip().lower()
    iden = re.sub(r"[^a-z0-9_]", "_", iden)
    return re.sub(r"_+", "_", iden).strip("_")


class ModbusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Modbus configuration flow."""

    VERSION = 4
    MINOR_VERSION = 0

    def __init__(self):
        """Initialize the config flow."""
        self._connection_type = None
        self._config_data = {}
        self._both_mode = False
        self._modbus_data = None
        self._cloud_data = None
        self._resolved_serial = None
        self._resolved_cloud_id = None
        self._cloud_product_model = None
        self._cloud_detected_model = None

    async def async_step_user(self, user_input=None):
        """Handle initial step - ask for connection type."""
        errors = {}

        if user_input is not None:
            # Save connection type and proceed to config step
            conn_type = user_input.get(CONF_CONNECTION_TYPE)
            if conn_type == CONN_TYPE_CLOUD:
                return await self.async_step_cloud()
            if conn_type == CONN_TYPE_BOTH:
                self._both_mode = True
                return await self.async_step_both_link()
            if conn_type:
                self._connection_type = conn_type
                return await self.async_step_config()

            errors["base"] = "Please select a connection type."

        # Show connection type selector only
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONNECTION_TYPE, default=CONN_TYPE_TCP): vol.In(CONNECTION_TYPES),
                }
            ),
            errors=errors,
        )

    async def async_step_both_link(self, user_input=None):
        """Dual-protocol setup: pick the Modbus link type first."""
        if user_input is not None:
            self._connection_type = user_input[CONF_CONNECTION_TYPE]
            return await self.async_step_config()
        return self.async_show_form(
            step_id="both_link",
            data_schema=vol.Schema({vol.Required(CONF_CONNECTION_TYPE, default=CONN_TYPE_TCP): vol.In(MODBUS_LINK_TYPES)}),
        )

    async def async_step_config(self, user_input=None):
        """Handle second step - show connection-specific configuration."""
        errors = {}

        if user_input is not None:
            # Merge connection type with configuration
            full_config = {CONF_CONNECTION_TYPE: self._connection_type, **user_input}

            if self._both_mode:
                # Validate the Modbus half, stash it, then collect the cloud half
                if CONF_INVERTER_SERIAL in full_config:
                    full_config[CONF_INVERTER_SERIAL] = str(full_config[CONF_INVERTER_SERIAL]).upper()
                valid, err_msg = await self._validate_config(full_config)
                if not valid:
                    errors["base"] = err_msg or "Cannot connect to Modbus device. Please check your configuration."
                else:
                    self._modbus_data = full_config
                    return await self.async_step_cloud()
            else:
                return await self._create_entry_from_input(full_config)

        # Remove connection_type from schema since we already have it
        if self._connection_type == CONN_TYPE_TCP:
            # Create TCP schema without connection_type field
            schema_dict = {k: v for k, v in TCP_CONFIG_SCHEMA.items() if not (hasattr(k, "schema") and k.schema == CONF_CONNECTION_TYPE)}
            schema = vol.Schema(schema_dict)
        else:
            # Create Serial schema without connection_type field
            schema_dict = {k: v for k, v in SERIAL_CONFIG_SCHEMA.items() if not (hasattr(k, "schema") and k.schema == CONF_CONNECTION_TYPE)}
            schema = vol.Schema(schema_dict)

        return self.async_show_form(step_id="config", data_schema=schema, errors=errors)

    async def async_step_cloud(self, user_input=None):
        """Collect + validate SolisCloud credentials (cloud-only entry, or the cloud half of a dual entry)."""
        errors = {}

        if user_input is not None:
            if self._both_mode:
                # The dual entry is keyed by the Modbus serial; the cloud plant must contain it
                user_input = {**user_input, CONF_INVERTER_SERIAL: self._modbus_data[CONF_INVERTER_SERIAL]}
            serial, inverter_id, detected_model, product_model, detected_has_v2, error = await self._validate_cloud_config(user_input)
            if error:
                errors["base"] = error
            elif self._both_mode:
                if detected_model and self._modbus_data.get("model") != detected_model:
                    _LOGGER.info(
                        "SolisCloud model metadata identifies %s; overriding selected model %s for serial %s",
                        detected_model,
                        self._modbus_data.get("model"),
                        serial,
                    )
                    self._modbus_data["model"] = detected_model
                if detected_has_v2 is not None and self._modbus_data.get("has_v2") != detected_has_v2:
                    _LOGGER.info(
                        "HMI firmware version identifies TOU V2 support=%s; overriding has_v2=%s for serial %s",
                        detected_has_v2,
                        self._modbus_data.get("has_v2"),
                        serial,
                    )
                    self._modbus_data["has_v2"] = detected_has_v2
                self._cloud_data = user_input
                self._resolved_serial = serial
                self._resolved_cloud_id = inverter_id
                self._cloud_product_model = product_model
                self._cloud_detected_model = detected_model
                return await self.async_step_dual_mode()
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                if detected_model and user_input.get("model") != detected_model:
                    _LOGGER.info(
                        "SolisCloud model metadata identifies %s; overriding selected model %s for serial %s",
                        detected_model,
                        user_input.get("model"),
                        serial,
                    )
                if detected_has_v2 is not None and user_input.get("has_v2") != detected_has_v2:
                    _LOGGER.info(
                        "HMI firmware version identifies TOU V2 support=%s; overriding has_v2=%s for serial %s",
                        detected_has_v2,
                        user_input.get("has_v2"),
                        serial,
                    )
                data = {
                    **user_input,
                    "model": detected_model or user_input.get("model"),
                    "has_v2": detected_has_v2 if detected_has_v2 is not None else user_input.get("has_v2"),
                    CONF_INVERTER_SERIAL: serial,
                    CONF_CLOUD_INVERTER_ID: inverter_id,
                    CONF_CLOUD_PRODUCT_MODEL: product_model,
                    CONF_CLOUD_DETECTED_MODEL: detected_model,
                    CONF_CONNECTION_TYPE: CONN_TYPE_CLOUD,
                    CONF_PROTOCOL_MODE: PROTO_CLOUD_ONLY,
                }
                return self.async_create_entry(title=f"Solis: {serial} (Cloud)", data=data)

        schema = dict(CLOUD_CONFIG_SCHEMA)
        if self._both_mode:
            # Model/features and serial come from the Modbus step; only credentials are needed
            for key in list(schema):
                key_name = getattr(key, "schema", key)
                if key_name in (
                    "model",
                    "has_v2",
                    "has_pv",
                    "has_ac_coupling",
                    "has_parallel",
                    "has_battery",
                    "has_hv_battery",
                    "has_generator",
                    CONF_INVERTER_SERIAL,
                ):
                    del schema[key]

        return self.async_show_form(
            step_id="cloud",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), user_input or {}),
            errors=errors,
        )

    async def async_step_dual_mode(self, user_input=None):
        """Final dual-protocol step: failover direction or manual switching."""
        if user_input is not None:
            choice = user_input["dual_mode"]
            protocol_mode = PROTO_BOTH_MANUAL if choice == "manual" else PROTO_BOTH_FAILOVER
            primary = PROTOCOL_CLOUD if choice == "failover_cloud" else PROTOCOL_MODBUS

            serial = self._resolved_serial
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            data = {
                **self._modbus_data,
                **self._cloud_data,
                CONF_INVERTER_SERIAL: serial,
                CONF_CLOUD_INVERTER_ID: self._resolved_cloud_id,
                CONF_CLOUD_PRODUCT_MODEL: self._cloud_product_model,
                CONF_CLOUD_DETECTED_MODEL: self._cloud_detected_model,
                CONF_PROTOCOL_MODE: protocol_mode,
                CONF_FAILOVER_PRIMARY: primary,
            }
            return self.async_create_entry(title=f"Solis: {serial} (Modbus + Cloud)", data=data)

        return self.async_show_form(
            step_id="dual_mode",
            data_schema=vol.Schema({vol.Required("dual_mode", default="failover_modbus"): vol.In(DUAL_MODE_OPTIONS)}),
        )

    async def _validate_cloud_config(self, user_input) -> tuple[str | None, str | None, str | None, str | None, bool | None, str | None]:
        """Check credentials against the SolisCloud API and resolve the inverter serial + cloud id.

        Returns (serial, inverter_id, detected_model, product_model, detected_has_v2, None) on success.
        The cloud id is required alongside the serial for inverterDetail reads.
        """
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        from .cloud.api_client import SolisCloudApiClient, SolisCloudApiError
        from .cloud.model_detection import cloud_model_metadata, infer_model_from_cloud_record

        client = SolisCloudApiClient(
            session=async_get_clientsession(self.hass),
            key_id=user_input[CONF_CLOUD_KEY_ID].strip(),
            key_secret=user_input[CONF_CLOUD_KEY_SECRET].strip(),
            username=(user_input.get(CONF_CLOUD_USERNAME) or "").strip() or None,
            password=(user_input.get(CONF_CLOUD_PASSWORD) or "").strip() or None,
        )

        try:
            records = await client.async_inverter_list(user_input[CONF_CLOUD_PLANT_ID].strip())
        except SolisCloudApiError as error:
            _LOGGER.warning("SolisCloud validation failed: %s", error)
            return None, None, None, None, None, "Cannot reach the SolisCloud API with these credentials. Check the key id/secret and plant id."

        by_serial = {str(rec["sn"]).upper(): rec for rec in records if rec.get("sn")}
        if not by_serial:
            return None, None, None, None, None, "No inverters found for this plant id."

        async def _resolve(serial: str, record: dict):
            metadata = cloud_model_metadata(record)
            product_model = metadata.get("productModel") or metadata.get("model")
            inverter_id = str(record.get("id") or "") or None
            detected_has_v2 = None
            # inverterList doesn't include hmiVersionAll; that field is only in inverterDetail
            # (the same call cloud_retrieval.py already makes every poll cycle).
            try:
                detail = await client.async_inverter_detail(serial, inverter_id)
            except SolisCloudApiError as error:
                _LOGGER.debug("HMI version lookup failed for serial %s during setup: %s", serial, error)
            else:
                detected_has_v2 = hmi_version_supports_v2(detail.get("hmiVersionAll"))
            return serial, inverter_id, infer_model_from_cloud_record(record), product_model, detected_has_v2, None

        wanted = (user_input.get(CONF_INVERTER_SERIAL) or "").strip().upper()
        if wanted:
            if wanted not in by_serial:
                return None, None, None, None, None, f"Inverter serial {wanted} not found in this plant (found: {', '.join(by_serial)})."
            return await _resolve(wanted, by_serial[wanted])
        if len(by_serial) == 1:
            serial, record = next(iter(by_serial.items()))
            return await _resolve(serial, record)
        return None, None, None, None, None, f"This plant has multiple inverters ({', '.join(by_serial)}); please enter the serial of the one to add."

    async def async_step_reconfigure(self, user_input=None):
        """Handle reconfiguration."""
        errors = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])

        # Cloud entries have no Modbus link to validate — use the cloud form instead.
        if entry.data.get(CONF_CONNECTION_TYPE) == CONN_TYPE_CLOUD:
            return await self.async_step_reconfigure_cloud(user_input)

        if user_input is not None:
            if CONF_INVERTER_SERIAL in user_input:
                user_input[CONF_INVERTER_SERIAL] = str(user_input[CONF_INVERTER_SERIAL]).strip().upper()

            # Update existing entry data with new input
            data = {**entry.data, **user_input}

            # Require serial and model so the form always collects them
            if not (data.get(CONF_INVERTER_SERIAL) or "").strip():
                errors["base"] = "Inverter serial number is required."
            elif not data.get("model"):
                errors["base"] = "Please select an inverter model."
            else:
                valid, err_msg = await self._validate_config(data)
                if valid:
                    return self.async_update_reload_and_abort(entry, data=data)
                errors["base"] = err_msg or "Cannot connect to Modbus device. Please check your configuration."

        # 1. Select the full schema (TCP or Serial) so reconfigure shows all fields
        conn_type = entry.data.get(CONF_CONNECTION_TYPE, CONN_TYPE_TCP)
        if conn_type == CONN_TYPE_TCP:
            source_schema = TCP_CONFIG_SCHEMA
        else:
            source_schema = SERIAL_CONFIG_SCHEMA

        # 2. Pre-fill form with current entry data/options (serial, model, host, etc.)
        current_config = {**entry.data, **entry.options}
        schema_with_suggestions = self.add_suggested_values_to_schema(vol.Schema(source_schema), current_config)

        return self.async_show_form(step_id="reconfigure", data_schema=schema_with_suggestions, errors=errors)

    async def async_step_reconfigure_cloud(self, user_input=None):
        """Reconfigure a SolisCloud entry (credentials, model, poll interval)."""
        errors = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])

        if user_input is not None:
            serial, inverter_id, detected_model, product_model, detected_has_v2, error = await self._validate_cloud_config(user_input)
            if error:
                errors["base"] = error
            elif serial != entry.unique_id:
                errors["base"] = f"Serial {serial} does not match this entry ({entry.unique_id}). Add a new entry for a different inverter."
            else:
                if detected_model and user_input.get("model") != detected_model:
                    _LOGGER.info(
                        "SolisCloud model metadata identifies %s; overriding selected model %s for serial %s",
                        detected_model,
                        user_input.get("model"),
                        serial,
                    )
                if detected_has_v2 is not None and user_input.get("has_v2") != detected_has_v2:
                    _LOGGER.info(
                        "HMI firmware version identifies TOU V2 support=%s; overriding has_v2=%s for serial %s",
                        detected_has_v2,
                        user_input.get("has_v2"),
                        serial,
                    )
                data = {
                    **entry.data,
                    **user_input,
                    "model": detected_model or user_input.get("model"),
                    "has_v2": detected_has_v2 if detected_has_v2 is not None else user_input.get("has_v2"),
                    CONF_INVERTER_SERIAL: serial,
                    CONF_CLOUD_INVERTER_ID: inverter_id,
                    CONF_CLOUD_PRODUCT_MODEL: product_model,
                    CONF_CLOUD_DETECTED_MODEL: detected_model,
                }
                return self.async_update_reload_and_abort(entry, data=data)

        current = {**entry.data, **entry.options}
        return self.async_show_form(
            step_id="reconfigure_cloud",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(CLOUD_CONFIG_SCHEMA), user_input or current),
            errors=errors,
        )

    async def _create_entry_from_input(self, data):
        """Validate and create the entry from input data."""
        errors = {}

        # 1. Sanitize Serial (Convert to Uppercase)
        if CONF_INVERTER_SERIAL in data:
            data[CONF_INVERTER_SERIAL] = str(data[CONF_INVERTER_SERIAL]).upper()

        # 2. Validate Connection
        valid, err_msg = await self._validate_config(data)
        if not valid:
            errors["base"] = err_msg or "Cannot connect to Modbus device. Please check your configuration."

            # Determine which schema to show again based on connection type
            if data.get(CONF_CONNECTION_TYPE) == CONN_TYPE_TCP:
                schema_dict = TCP_CONFIG_SCHEMA
            else:
                schema_dict = SERIAL_CONFIG_SCHEMA

            return self.async_show_form(step_id="config", data_schema=vol.Schema(schema_dict), errors=errors)

        # 2b. The serial field is optional because hybrid models auto-detect it during
        # validation above; grid/string models don't expose that register range, so if it's
        # still blank at this point there's no way to identify the device — ask for it.
        if not (data.get(CONF_INVERTER_SERIAL) or "").strip():
            errors["base"] = "Could not detect the inverter serial automatically; please enter it manually."
            if data.get(CONF_CONNECTION_TYPE) == CONN_TYPE_TCP:
                schema_dict = TCP_CONFIG_SCHEMA
            else:
                schema_dict = SERIAL_CONFIG_SCHEMA
            return self.async_show_form(step_id="config", data_schema=self.add_suggested_values_to_schema(vol.Schema(schema_dict), data), errors=errors)

        # 3. Check for Duplicates
        if CONF_INVERTER_SERIAL in data:
            await self.async_set_unique_id(data[CONF_INVERTER_SERIAL])
            self._abort_if_unique_id_configured()

        # 4. Create the Config Entry (v4 entries carry an explicit protocol mode)
        data.setdefault(CONF_PROTOCOL_MODE, PROTO_MODBUS_ONLY)
        return self.async_create_entry(title=f"Solis: {data[CONF_INVERTER_SERIAL]}", data=data)

    async def _validate_config(self, user_input):
        """Validate the configuration by trying to connect to the Modbus device.
        Returns (True, None) on success, (False, error_message) on failure.
        """
        inverter_model = user_input.get("model")
        inverter_template: InverterConfig | None = next((inv for inv in SOLIS_INVERTERS if inv.model == inverter_model), None)

        if inverter_template is None:
            _LOGGER.warning("Invalid or unknown inverter model: %s", inverter_model)
            return False, "Invalid or unknown inverter model. Please select a model from the list."

        user_options = inverter_options_from_config(user_input, inverter_template)
        inverter_config = inverter_template.clone_with_options(user_options, user_input.get("connection", "S2_WL_ST"))

        conn_type = user_input.get(CONF_CONNECTION_TYPE, CONN_TYPE_SERIAL)

        # Build ModbusController parameters based on connection type
        controller_params = {
            "hass": self.hass,
            "device_id": user_input.get("slave", 1),
            "identification": clean_identification(user_input.get("identification", None)),
            "fast_poll": user_input.get("poll_interval_fast", 10),
            "normal_poll": user_input.get("poll_interval_normal", 15),
            "slow_poll": user_input.get("poll_interval_slow", 15),
            "inverter_config": inverter_config,
            "connection_type": conn_type,
        }

        if conn_type == CONN_TYPE_TCP:
            controller_params["host"] = user_input["host"]
            controller_params["port"] = user_input.get("port", 502)
        else:  # Serial
            controller_params["serial_port"] = user_input[CONF_SERIAL_PORT]
            controller_params["baudrate"] = user_input.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)
            controller_params["bytesize"] = user_input.get(CONF_BYTESIZE, DEFAULT_BYTESIZE)
            controller_params["parity"] = user_input.get(CONF_PARITY, DEFAULT_PARITY)
            controller_params["stopbits"] = user_input.get(CONF_STOPBITS, DEFAULT_STOPBITS)

        modbus_controller = ModbusController(**controller_params)

        for attempt in range(5):
            try:
                if not await modbus_controller.connect():
                    raise ConnectionError("Failed to connect")
                if inverter_config.type in [InverterType.GRID, InverterType.STRING]:
                    await modbus_controller.async_read_input_register(3041, 1)
                else:
                    await modbus_controller.async_read_input_register(35000, 1)

                # Auto-detect TOU V2 support from HMI firmware version (register 33002) rather
                # than relying solely on the form's manual toggle. A failed/unparseable read
                # must never block setup; the user's chosen value survives untouched.
                try:
                    hmi_registers = await modbus_controller.async_read_input_register(33002, 1)
                except Exception as hmi_error:
                    _LOGGER.debug("HMI version read failed during setup; leaving has_v2 as configured: %s", hmi_error)
                else:
                    if hmi_registers:
                        detected_has_v2 = hmi_version_supports_v2(hmi_registers[0])
                        if detected_has_v2 is not None and user_input.get("has_v2") != detected_has_v2:
                            _LOGGER.info(
                                "HMI version %s identifies TOU V2 support=%s; overriding has_v2=%s for serial %s",
                                hmi_registers[0],
                                detected_has_v2,
                                user_input.get("has_v2"),
                                user_input.get(CONF_INVERTER_SERIAL),
                            )
                            user_input["has_v2"] = detected_has_v2

                # Auto-detect the inverter serial from the device (registers 33004-33019,
                # ASCII) rather than trusting only what the installer typed. Grid/string
                # models don't expose this range, so skip there. A failed/garbage read must
                # never block setup; the user's typed serial survives untouched.
                if inverter_config.type not in [InverterType.GRID, InverterType.STRING]:
                    try:
                        serial_registers = await modbus_controller.async_read_input_register(33004, 16)
                    except Exception as serial_error:
                        _LOGGER.debug("Serial number read failed during setup; leaving serial as configured: %s", serial_error)
                    else:
                        if serial_registers:
                            detected_serial = extract_serial_number(serial_registers).strip().upper()
                            if detected_serial and detected_serial != (user_input.get(CONF_INVERTER_SERIAL) or "").strip().upper():
                                _LOGGER.info(
                                    "Detected inverter serial %s from device; overriding configured serial %s",
                                    detected_serial,
                                    user_input.get(CONF_INVERTER_SERIAL),
                                )
                                user_input[CONF_INVERTER_SERIAL] = detected_serial

                return True, None
            except Exception as e:
                _LOGGER.warning(f"Connection failed attempt {attempt + 1}/5: {str(e)}")
                if attempt < 4:
                    await asyncio.sleep(1)
            finally:
                modbus_controller.close_connection()

        _LOGGER.error(f"Connection failed after 5 attempts: {str(controller_params)}")
        return False, "Cannot connect to Modbus device. Please check your configuration."

    def _get_user_schema(self, user_input=None):
        """Return the appropriate schema based on connection type selection."""
        if user_input and CONF_CONNECTION_TYPE in user_input:
            conn_type = user_input[CONF_CONNECTION_TYPE]
            if conn_type == CONN_TYPE_TCP:
                return vol.Schema(TCP_CONFIG_SCHEMA)
            else:
                return vol.Schema(SERIAL_CONFIG_SCHEMA)

        return vol.Schema(SERIAL_CONFIG_SCHEMA)

    @staticmethod
    @config_entries.HANDLERS.register(DOMAIN)
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return ModbusOptionsFlowHandler()


CLOUD_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLOUD_POLL_INTERVAL, default=DEFAULT_CLOUD_POLL_INTERVAL): vol.All(int, vol.Range(min=MIN_CLOUD_POLL_INTERVAL)),
        vol.Required("model"): vol.In(SOLIS_MODELS),
        vol.Required("has_v2", default=True): bool,
        vol.Required("has_pv", default=True): bool,
        vol.Required("has_ac_coupling", default=False): bool,
        vol.Required("has_parallel", default=False): bool,
        vol.Required("has_battery", default=True): bool,
        vol.Required("has_hv_battery", default=False): bool,
        vol.Required("has_generator", default=False): bool,
    }
)


class ModbusOptionsFlowHandler(OptionsFlow):
    """Handle options flow for Modbus."""

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}

        if user_input is not None:
            merged = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=merged)

        # Cloud entries have one poll interval and no Modbus link options
        schema = CLOUD_OPTIONS_SCHEMA if self.config_entry.data.get(CONF_CONNECTION_TYPE) == CONN_TYPE_CLOUD else OPTIONS_SCHEMA

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            errors=errors,
        )
