from custom_components.solisconnect.data.enums import InverterFeature, InverterType
from custom_components.solisconnect.data.solis_config import (
    SOLIS_INVERTERS,
    InverterConfig,
    InverterOptions,
    inverter_options_from_config,
)
from custom_components.solisconnect.sensor_data.switch_sensors import get_switch_sensors


def _switch_names(inverter_config: InverterConfig) -> set[str]:
    return {entity["name"] for group in get_switch_sensors(inverter_config) for entity in group["entities"]}


def _switch_entities(inverter_config: InverterConfig, register: int) -> list[dict]:
    return [entity for group in get_switch_sensors(inverter_config) if group.get("register") == register for entity in group["entities"]]


def test_ac_coupling_feature_enabled():
    """Test that AC_COUPLING feature is added when option is enabled."""
    options = InverterOptions(ac_coupling=True)
    config = InverterConfig(model="S6-EH1P", wattage=[8000], phases=1, type=InverterType.HYBRID, options=options)

    assert InverterFeature.AC_COUPLING in config.features


def test_ac_coupling_feature_disabled_by_default():
    """Test that AC_COUPLING feature is NOT added by default."""
    config = InverterConfig(model="S6-EH1P", wattage=[8000], phases=1, type=InverterType.HYBRID)

    assert InverterFeature.AC_COUPLING not in config.features


def test_generator_feature_disabled_by_default():
    """Generator controls must be opt-in, matching the config-flow default."""
    config = InverterConfig(model="S6-EH1P", wattage=[8000], phases=1, type=InverterType.HYBRID)

    assert InverterFeature.GENERATOR not in config.features


def test_inverter_options_generator_default_is_false():
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")

    options = inverter_options_from_config({}, template)

    assert options.generator is False


def test_clone_applies_user_options_and_leaves_templates_untouched():
    """User options must rebuild features; SOLIS_INVERTERS entries must stay immutable."""
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    feats_before = list(template.features)

    user = {
        "has_v2": True,
        "has_pv": True,
        "has_ac_coupling": True,
        "has_parallel": False,
        "has_battery": True,
        "has_hv_battery": False,
        "has_generator": True,
    }
    clone = template.clone_with_options(inverter_options_from_config(user, template), "S2_WL_ST")

    assert InverterFeature.AC_COUPLING in clone.features
    assert InverterFeature.GENERATOR in clone.features
    assert template.features == feats_before
    assert InverterFeature.AC_COUPLING not in template.features


def test_hybrid_sensors_ac_coupling_requirement():
    """Test that some hybrid sensors require AC_COUPLING feature."""
    from custom_components.solisconnect.sensor_data.hybrid_sensors import hybrid_sensors

    ac_coupling_groups = [group for group in hybrid_sensors if group.get("feature_requirement") and InverterFeature.AC_COUPLING in group["feature_requirement"]]

    assert len(ac_coupling_groups) > 0
    for group in ac_coupling_groups:
        assert InverterFeature.AC_COUPLING in group["feature_requirement"]


def test_generator_switches_hidden_without_generator_feature():
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(generator=False, ac_coupling=False), "S2_WL_ST")

    names = _switch_names(config)

    assert all("Generator" not in name for name in names)
    assert "AC Coupling Position (off = GEN port, on = Backup port)" not in names
    assert "AC Coupling Enable" not in names


def test_generator_switches_enabled_with_generator_feature():
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(generator=True, ac_coupling=False), "S2_WL_ST")

    names = _switch_names(config)

    assert "With Generator" in names
    assert "Generator Charge Enable" in names
    assert "Force Start Generator" in names
    assert "AC Coupling Enable" not in names


def test_ac_coupling_switches_enabled_with_ac_coupling_feature():
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(generator=False, ac_coupling=True), "S2_WL_ST")

    names = _switch_names(config)

    assert "AC Coupling Position (off = GEN port, on = Backup port)" in names
    assert "AC Coupling Enable" in names
    assert "With Generator" not in names


def test_mode_switch_names_and_disabled_bits():
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(), "S2_WL_ST")

    by_bit = {entity["bit_position"]: entity for entity in _switch_entities(config, 43110)}

    assert by_bit[0]["name"] == "Mode - Self Use"
    # Bit 1 (Timed Charge / TOU) is intentionally NOT a switch entity: it does not persist
    # on this firmware family (see sensor_data/binary_sensors.py for the read-only status).
    assert 1 not in by_bit
    assert by_bit[2]["name"] == "Mode - Off-Grid"
    assert by_bit[3]["name"] == "Mode - Battery Wake Up"
    assert by_bit[4]["name"] == "Mode - Backup"
    assert by_bit[5]["name"] == "Mode - Grid Charge"
    assert by_bit[6]["name"] == "Mode - Feed In Priority"
    assert by_bit[7]["name"] == "Mode - Night OVD Retain"
    assert by_bit[10]["name"] == "Mode - Battery Condition"
    assert by_bit[11]["name"] == "Mode - Peak Shaving"
    assert by_bit[8]["enabled"] is False
    assert by_bit[9]["enabled"] is False


def test_timed_charge_enable_switch_names():
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(), "S2_WL_ST")

    names = [entity["name"] for entity in _switch_entities(config, 43707)]

    # Bits 0-5 are the 6 charge slots, bits 6-11 the 6 discharge slots.
    assert names == [f"Timed Charge Enable {slot}" for slot in range(1, 7)] + [f"Timed Discharge Enable {slot}" for slot in range(1, 7)]


def test_timed_slot_number_names_are_normalized():
    from custom_components.solisconnect.sensor_data.hybrid_sensors import hybrid_sensors

    entities = {int(entity["register"][0]): entity for group in hybrid_sensors for entity in group.get("entities", []) if entity.get("register")}

    assert entities[43708]["name"] == "Timed Charge SOC 1"
    assert entities[43709]["name"] == "Timed Charge Current 1"
    assert entities[43710]["name"] == "Timed Charge Voltage 1"
    assert entities[43750]["name"] == "Timed Discharge SOC 1"
    assert entities[43751]["name"] == "Timed Discharge Current 1"
    assert entities[43752]["name"] == "Timed Discharge Voltage 1"
    assert entities[43785]["name"] == "Timed Discharge SOC 6"
    assert entities[43786]["name"] == "Timed Discharge Current 6"
    assert entities[43787]["name"] == "Timed Discharge Voltage 6"
    assert entities[43024]["name"] == "Backup Mode SOC"


def test_timed_slot_time_names_are_normalized():
    from custom_components.solisconnect.sensor_data.time_sensors import get_time_sensors

    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH1P")
    config = template.clone_with_options(InverterOptions(), "S2_WL_ST")
    entities = {entity["register"]: entity for entity in get_time_sensors(config)}

    assert entities[43711]["name"] == "Timed Charge Start 1"
    assert entities[43713]["name"] == "Timed Charge End 1"
    assert entities[43753]["name"] == "Timed Discharge Start 1"
    assert entities[43755]["name"] == "Timed Discharge End 1"
    assert entities[43788]["name"] == "Timed Discharge Start 6"
    assert entities[43790]["name"] == "Timed Discharge End 6"


def test_parallel_feature_disabled_by_default():
    """PARALLEL is opt-in; default installs must not advertise it on the template."""
    config = InverterConfig(model="S5-EH1P", wattage=[5000], phases=1, type=InverterType.HYBRID)

    assert InverterFeature.PARALLEL not in config.features


def test_parallel_feature_when_option_enabled():
    options = InverterOptions(parallel=True)
    config = InverterConfig(model="S5-EH1P", wattage=[5000], phases=1, type=InverterType.HYBRID, options=options)

    assert InverterFeature.PARALLEL in config.features


def test_hybrid_sensors_parallel_sync_block_gated():
    """Parallel synchronization result (34243) must only load when PARALLEL is enabled."""
    from custom_components.solisconnect.sensor_data.hybrid_sensors import hybrid_sensors

    parallel_groups = [group for group in hybrid_sensors if group.get("register_start") == 34243 and group.get("feature_requirement")]
    assert len(parallel_groups) == 1
    assert InverterFeature.PARALLEL in parallel_groups[0]["feature_requirement"]
