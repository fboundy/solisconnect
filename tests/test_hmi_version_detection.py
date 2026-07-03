"""Unit tests for the shared HMI-version-to-V2-support helper (issue #20)."""

from custom_components.solisconnect.helpers import V2_HMI_VERSION_THRESHOLD, hmi_version_supports_v2


def test_threshold_is_0x4b00():
    assert V2_HMI_VERSION_THRESHOLD == 0x4B00


def test_at_threshold_supports_v2():
    assert hmi_version_supports_v2(0x4B00) is True


def test_above_threshold_supports_v2():
    assert hmi_version_supports_v2(0x5000) is True


def test_below_threshold_does_not_support_v2():
    assert hmi_version_supports_v2(0x4AFF) is False


def test_accepts_cloud_hex_string():
    assert hmi_version_supports_v2("4B00") is True
    assert hmi_version_supports_v2("4AFF") is False


def test_accepts_modbus_int():
    assert hmi_version_supports_v2(19200) is True  # 0x4B00 decimal


def test_none_is_undeterminable():
    assert hmi_version_supports_v2(None) is None


def test_unparseable_string_is_undeterminable():
    assert hmi_version_supports_v2("not-hex") is None


def test_unparseable_type_is_undeterminable():
    assert hmi_version_supports_v2(object()) is None
