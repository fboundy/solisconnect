from custom_components.solisconnect.cloud.model_detection import infer_model_from_cloud_record


def test_infer_model_from_cloud_product_code():
    assert infer_model_from_cloud_record({"productModel": "3102"}) == "S5-EH1P"


def test_infer_model_from_cloud_textual_model():
    assert infer_model_from_cloud_record({"model": "Solis S6-EH3P10K-H-ZP"}) == "S6-EH3P10K-H-ZP"


def test_infer_model_unknown_returns_none():
    assert infer_model_from_cloud_record({"productModel": "not-known"}) is None
