DOMAIN = "solisconnect"
CONTROLLER = "modbus_controller"
SLAVE = "modbus_controller_slave"
MANUFACTURER = "Solis"

VALUES = "values"
VALUE = "value"
REGISTER = "register"
SCOPE = "scope"
SENSOR_ENTITIES = "sensor_entities"
TIME_ENTITIES = "time_entities"
SWITCH_ENTITIES = "switch_entities"
NUMBER_ENTITIES = "number_entities"
SENSOR_DERIVED_ENTITIES = "sensor_derived_entities"
DRIFT_COUNTER = "drift_counter"
ENTITIES = "entities"

# Connection types
CONN_TYPE_TCP = "tcp"
CONN_TYPE_SERIAL = "serial"
CONN_TYPE_CLOUD = "cloud"

# Protocol selection (which transport(s) feed the shared entities)
CONF_PROTOCOL_MODE = "protocol_mode"
PROTO_MODBUS_ONLY = "modbus_only"
PROTO_CLOUD_ONLY = "cloud_only"
PROTO_BOTH_FAILOVER = "both_failover"
PROTO_BOTH_MANUAL = "both_manual"

# Dual-protocol orchestration
CONF_FAILOVER_PRIMARY = "failover_primary"
PROTOCOL_MODBUS = "modbus"
PROTOCOL_CLOUD = "cloud"

# SolisCloud API configuration
CONF_CLOUD_USERNAME = "cloud_username"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_CLOUD_KEY_ID = "cloud_key_id"
CONF_CLOUD_KEY_SECRET = "cloud_key_secret"
CONF_CLOUD_PLANT_ID = "cloud_plant_id"
CONF_CLOUD_INVERTER_ID = "cloud_inverter_id"
CONF_CLOUD_POLL_INTERVAL = "cloud_poll_interval"
DEFAULT_CLOUD_POLL_INTERVAL = 300
MIN_CLOUD_POLL_INTERVAL = 60

# Serial connection parameters
CONF_SERIAL_PORT = "serial_port"
CONF_BAUDRATE = "baudrate"
CONF_BYTESIZE = "bytesize"
CONF_PARITY = "parity"
CONF_STOPBITS = "stopbits"
CONF_CONNECTION_TYPE = "connection_type"
CONF_INVERTER_SERIAL = "inverter_serial"
CONF_SLAVE = "slave"

# Default serial values (standard for Solis inverters)
DEFAULT_BAUDRATE = 9600
DEFAULT_BYTESIZE = 8
DEFAULT_PARITY = "N"
DEFAULT_STOPBITS = 1
