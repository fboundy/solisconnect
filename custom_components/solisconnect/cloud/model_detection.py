"""Helpers for deriving a SolisConnect model from SolisCloud metadata."""

from __future__ import annotations

import re

from custom_components.solisconnect.data.solis_config import SOLIS_INVERTERS

MODEL_NAMES = tuple(sorted((inverter.model for inverter in SOLIS_INVERTERS), key=len, reverse=True))

# Verified live: SolisCloud product code 3102 reports an S5-EH1P inverter.
PRODUCT_CODE_TO_MODEL = {
    "3102": "S5-EH1P",
}

MODEL_FIELDS = (
    "model",
    "productModel",
    "product_model",
    "inverterType",
    "inverter_type",
    "machine",
    "productName",
    "product_name",
    "typeName",
    "type_name",
)


def cloud_model_metadata(record: dict | None) -> dict[str, str]:
    """Return useful cloud model-identifying fields as strings."""
    if not isinstance(record, dict):
        return {}
    metadata = {}
    for key in MODEL_FIELDS:
        value = record.get(key)
        if value not in (None, ""):
            metadata[key] = str(value)
    return metadata


def infer_model_from_cloud_record(record: dict | None) -> str | None:
    """Infer the closest configured model from SolisCloud inverter metadata."""
    metadata = cloud_model_metadata(record)
    if not metadata:
        return None

    haystack = " ".join(metadata.values()).upper()
    for model in MODEL_NAMES:
        if model.upper() in haystack:
            return model

    for value in metadata.values():
        normalized = re.sub(r"\D", "", value)
        if normalized in PRODUCT_CODE_TO_MODEL:
            return PRODUCT_CODE_TO_MODEL[normalized]

    return None
