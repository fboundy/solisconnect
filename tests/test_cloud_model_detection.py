import logging

from custom_components.solisconnect.cloud.model_detection import infer_model_from_cloud_record


def test_infer_model_from_cloud_product_code():
    assert infer_model_from_cloud_record({"productModel": "3102"}) == "S5-EH1P"


def test_infer_model_from_cloud_textual_model():
    assert infer_model_from_cloud_record({"model": "Solis S6-EH3P10K-H-ZP"}) == "S6-EH3P10K-H-ZP"


def test_infer_model_unknown_returns_none():
    assert infer_model_from_cloud_record({"productModel": "not-known"}) is None


def test_infer_model_unknown_logs_warning_for_reporting(caplog):
    """Issue #17: an unrecognized product code should be surfaced so users can report it."""
    with caplog.at_level(logging.WARNING, logger="custom_components.solisconnect.cloud.model_detection"):
        assert infer_model_from_cloud_record({"productModel": "9999"}) is None
    assert any("does not match any known model" in record.message for record in caplog.records)
    assert any("9999" in record.message for record in caplog.records)


def test_infer_model_known_product_code_does_not_log_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="custom_components.solisconnect.cloud.model_detection"):
        assert infer_model_from_cloud_record({"productModel": "3102"}) == "S5-EH1P"
    assert caplog.records == []
