from homeassistant.helpers.entity import EntityCategory

from custom_components.solisconnect.data.enums import Category

SETTING_CATEGORIES = {
    Category.BASIC_SETTING,
    Category.BATTERY_SETTING,
    Category.AC_PORT_SETTING,
    Category.GRID_CODE_SETTING,
    Category.POWER_CONTROL_SETTING,
    Category.FUNCTIONAL_SETTING,
    Category.HYBRID_MODE_SETTING,
    Category.BACKUP_PORT_SETTING,
    Category.REMOTE_CONTROL_SETTING,
    Category.SMART_PORT_SETTING,
    Category.GENERATOR_SETTING,
    Category.HISTORY_DATA_QUERY_SETTING,
    Category.REMOTE_DISPATCH_SETTING,
}


def entity_category_for_sensor_category(
    category: Category | None,
    control: bool = False,
    *,
    config_allowed: bool = True,
) -> EntityCategory | None:
    """Map a Solis sensor category onto a Home Assistant entity category.

    The sensor and binary_sensor domains reject EntityCategory.CONFIG outright, so
    read-only entities in those domains must pass config_allowed=False and land in
    DIAGNOSTIC instead. Writable domains (number, select, switch, time) keep CONFIG.
    """
    if control:
        return None
    if category in SETTING_CATEGORIES:
        return EntityCategory.CONFIG if config_allowed else EntityCategory.DIAGNOSTIC
    return None
