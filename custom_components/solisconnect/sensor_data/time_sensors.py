from custom_components.solisconnect.data.enums import InverterFeature, InverterType


def get_time_sensors(inverter_config):
    time_definitions = []

    if inverter_config.type == InverterType.HYBRID:
        time_definitions = [
            {"name": "Time-Charging Charge Start (Slot 1)", "register": 43143, "enabled": True},
            {"name": "Time-Charging Charge End (Slot 1)", "register": 43145, "enabled": True},
            {"name": "Time-Charging Discharge Start (Slot 1)", "register": 43147, "enabled": True},
            {"name": "Time-Charging Discharge End (Slot 1)", "register": 43149, "enabled": True},
            {"name": "Time-Charging Charge Start (Slot 2)", "register": 43153, "enabled": True},
            {"name": "Time-Charging Charge End (Slot 2)", "register": 43155, "enabled": True},
            {"name": "Time-Charging Discharge Start (Slot 2)", "register": 43157, "enabled": True},
            {"name": "Time-Charging Discharge End (Slot 2)", "register": 43159, "enabled": True},
            {"name": "Time-Charging Charge Start (Slot 3)", "register": 43163, "enabled": True},
            {"name": "Time-Charging Charge End (Slot 3)", "register": 43165, "enabled": True},
            {"name": "Time-Charging Discharge Start (Slot 3)", "register": 43167, "enabled": True},
            {"name": "Time-Charging Discharge End (Slot 3)", "register": 43169, "enabled": True},
        ]

    if inverter_config.type == InverterType.HYBRID or InverterFeature.V2 in inverter_config.features:
        time_definitions.extend(
            [
                {"name": "Timed Charge Start 1", "register": 43711, "enabled": True, "control": True},
                {"name": "Timed Charge End 1", "register": 43713, "enabled": True, "control": True},
                {"name": "Timed Discharge Start 1", "register": 43753, "enabled": True, "control": True},
                {"name": "Timed Discharge End 1", "register": 43755, "enabled": True, "control": True},
                {"name": "Timed Charge Start 2", "register": 43718, "enabled": True, "control": True},
                {"name": "Timed Charge End 2", "register": 43720, "enabled": True, "control": True},
                {"name": "Timed Discharge Start 2", "register": 43760, "enabled": True, "control": True},
                {"name": "Timed Discharge End 2", "register": 43762, "enabled": True, "control": True},
                {"name": "Timed Charge Start 3", "register": 43725, "enabled": True, "control": True},
                {"name": "Timed Charge End 3", "register": 43727, "enabled": True, "control": True},
                {"name": "Timed Discharge Start 3", "register": 43767, "enabled": True, "control": True},
                {"name": "Timed Discharge End 3", "register": 43769, "enabled": True, "control": True},
                {"name": "Timed Charge Start 4", "register": 43732, "enabled": True, "control": True},
                {"name": "Timed Charge End 4", "register": 43734, "enabled": True, "control": True},
                {"name": "Timed Discharge Start 4", "register": 43774, "enabled": True, "control": True},
                {"name": "Timed Discharge End 4", "register": 43776, "enabled": True, "control": True},
                {"name": "Timed Charge Start 5", "register": 43739, "enabled": True, "control": True},
                {"name": "Timed Charge End 5", "register": 43741, "enabled": True, "control": True},
                {"name": "Timed Discharge Start 5", "register": 43781, "enabled": True, "control": True},
                {"name": "Timed Discharge End 5", "register": 43783, "enabled": True, "control": True},
                {"name": "Timed Charge Start 6", "register": 43746, "enabled": True, "control": True},
                {"name": "Timed Charge End 6", "register": 43748, "enabled": True, "control": True},
                {"name": "Timed Discharge Start 6", "register": 43788, "enabled": True, "control": True},
                {"name": "Timed Discharge End 6", "register": 43790, "enabled": True, "control": True},
            ]
        )

    # Add unique key to all - unique key based on register
    for x in time_definitions:
        x["unique"] = f"time_entity_{x['register']}"

    return time_definitions
