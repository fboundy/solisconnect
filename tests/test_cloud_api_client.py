"""Tests for the SolisCloud API client (signing, retries, token caching, rate spacing)."""

from unittest.mock import patch

import pytest
from aiohttp import ClientSession, TCPConnector
from aiohttp.resolver import ThreadedResolver
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.solis_modbus.cloud.api_client import (
    API_BASE_URL,
    MAX_ATTEMPTS,
    RESOURCE_AT_READ,
    RESOURCE_AT_READ_BATCH,
    RESOURCE_CONTROL,
    RESOURCE_INVERTER_DETAIL,
    RESOURCE_INVERTER_LIST,
    RESOURCE_LOGIN,
    SolisCloudApiClient,
    SolisCloudApiError,
    SolisCloudAuthError,
)

KEY_ID = "test_key_id"
KEY_SECRET = "test_secret"
SN = "1031234567890001"


@pytest.fixture
async def client_factory(hass, aioclient_mock: AiohttpClientMocker):
    """Build clients on mocked sessions; closes the sessions on teardown.

    Uses a thread-based DNS resolver because the default aiodns resolver refuses to
    construct under the Windows proactor event loop — the session never touches the
    network anyway (requests are served by the mocker).
    """
    sessions: list[ClientSession] = []

    def factory(**kwargs) -> SolisCloudApiClient:
        session = ClientSession(connector=TCPConnector(resolver=ThreadedResolver()))
        object.__setattr__(session, "_request", aioclient_mock.match_request)
        sessions.append(session)
        client = SolisCloudApiClient(session=session, key_id=KEY_ID, key_secret=KEY_SECRET, **kwargs)
        client._last_request_monotonic = 0.0  # no spacing waits in unit tests
        return client

    yield factory

    for session in sessions:
        await session.close()


def test_signing_known_vector():
    """Signature literals precomputed once from the documented algorithm; pins byte-exactness."""
    client = SolisCloudApiClient(session=None, key_id=KEY_ID, key_secret=KEY_SECRET)
    body = '{"inverterSn":"1031234567890001","cid":"636"}'
    headers = client._signed_headers(body, RESOURCE_AT_READ, date="Thu, 02 Jul 2026 12:00:00 GMT")

    assert headers["Content-MD5"] == "EqQfPZYY3Ithcmvik2GIJA=="
    assert headers["Content-Type"] == "application/json"
    assert headers["Date"] == "Thu, 02 Jul 2026 12:00:00 GMT"
    assert headers["Authorization"] == "API test_key_id:rDdHi1AYLnJd8vQCvGLXCgNL3Po="


def test_body_serialization_matches_signed_bytes():
    """The signature covers exactly the bytes sent: compact JSON, insertion-ordered."""
    body = SolisCloudApiClient._build_body({"inverterSn": SN, "cid": "636"})
    assert body == '{"inverterSn":"1031234567890001","cid":"636"}'


def test_date_header_is_locale_independent():
    client = SolisCloudApiClient(session=None, key_id=KEY_ID, key_secret=KEY_SECRET)
    headers = client._signed_headers("{}", RESOURCE_LOGIN)
    # RFC 1123 with English day/month names and GMT suffix regardless of system locale
    assert headers["Date"].endswith(" GMT")
    assert headers["Date"][:3] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


async def test_inverter_list_success(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(
        f"{API_BASE_URL}{RESOURCE_INVERTER_LIST}",
        json={"code": "0", "msg": "success", "data": {"page": {"records": [{"id": "123", "sn": SN}]}}},
    )
    client = client_factory()
    records = await client.async_inverter_list("plant1")
    assert records == [{"id": "123", "sn": SN}]

    # Signed headers were sent
    call_headers = aioclient_mock.mock_calls[0][3]
    assert call_headers["Authorization"].startswith(f"API {KEY_ID}:")
    assert "Content-MD5" in call_headers and "Date" in call_headers


async def test_inverter_detail_success(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(
        f"{API_BASE_URL}{RESOURCE_INVERTER_DETAIL}",
        json={"code": "0", "data": {"pac": 3.5, "pacStr": "kW", "batteryCapacitySoc": 55}},
    )
    client = client_factory()
    detail = await client.async_inverter_detail(SN, "123")
    assert detail["pac"] == 3.5
    assert detail["batteryCapacitySoc"] == 55


async def test_api_level_error_raises(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_INVERTER_DETAIL}", json={"code": "B0115", "msg": "unsupported datalogger"})
    client = client_factory()
    with pytest.raises(SolisCloudApiError, match="B0115"):
        await client.async_inverter_detail(SN)


async def test_retry_then_succeed(aioclient_mock: AiohttpClientMocker, client_factory):
    responses = [
        {"status": 502, "json": {}},
        {"status": 200, "json": {"code": "0", "data": {"msg": "35"}}},
    ]

    async def _respond(method, url, data):
        resp = responses.pop(0)
        return AiohttpClientMockResponse(method=method, url=url, status=resp["status"], json=resp["json"])

    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_AT_READ}", side_effect=_respond)
    client = client_factory()
    with patch("custom_components.solis_modbus.cloud.api_client.asyncio.sleep"):
        value = await client.async_at_read(SN, 636)
    assert value == "35"
    assert aioclient_mock.call_count == 2


async def test_gives_up_after_max_attempts(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_AT_READ}", status=502, json={})
    client = client_factory()
    with patch("custom_components.solis_modbus.cloud.api_client.asyncio.sleep"), pytest.raises(SolisCloudApiError, match="failed after"):
        await client.async_at_read(SN, 636)
    assert aioclient_mock.call_count == MAX_ATTEMPTS


async def test_408_mentions_clock_skew(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_AT_READ}", status=408, json={})
    client = client_factory()
    with pytest.raises(SolisCloudApiError, match="clock"):
        await client.async_at_read(SN, 636)


async def test_login_caches_token_and_attaches_it(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_LOGIN}", json={"code": "0", "data": {"csrfToken": "tok123"}})
    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_AT_READ}", json={"code": "0", "data": {"msg": "35"}})

    client = client_factory(username="user@example.com", password="hunter2")
    await client.async_at_read(SN, 636)
    await client.async_at_read(SN, 636)

    # login called once (token cached), atRead called twice with the token header
    login_calls = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith(RESOURCE_LOGIN)]
    read_calls = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith(RESOURCE_AT_READ)]
    assert len(login_calls) == 1
    assert len(read_calls) == 2
    assert all(c[3].get("token") == "tok123" for c in read_calls)


async def test_login_without_credentials_raises(client_factory):
    client = client_factory()
    with pytest.raises(SolisCloudAuthError):
        await client.async_login()


async def test_reads_work_without_credentials(aioclient_mock: AiohttpClientMocker, client_factory):
    """HMAC alone is sufficient for reads (and mkuthan-style control); no login round-trip."""
    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_AT_READ}", json={"code": "0", "data": {"msg": "35"}})
    client = client_factory()
    assert await client.async_at_read(SN, 636) == "35"
    assert aioclient_mock.call_count == 1  # no login attempt


async def test_at_read_batch_parses_list_shape(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(
        f"{API_BASE_URL}{RESOURCE_AT_READ_BATCH}",
        json={"code": "0", "data": [{"cid": 636, "msg": "35"}, {"cid": 157, "msg": "20"}]},
    )
    client = client_factory()
    batch = await client.async_at_read_batch(SN, [636, 157])
    assert batch == {636: "35", 157: "20"}


async def test_at_read_batch_parses_live_nested_shape(aioclient_mock: AiohttpClientMocker, client_factory):
    """The real API wraps records in a nested list, uses string cids, and includes sysCommand metadata."""
    aioclient_mock.post(
        f"{API_BASE_URL}{RESOURCE_AT_READ_BATCH}",
        json={
            "code": "0",
            "data": [
                [
                    {"msg": "33", "yuanzhi": "33", "cid": "636", "sysCommand": {"registerAddr": 43110}},
                    {"msg": "52", "yuanzhi": "52", "cid": "157", "sysCommand": {"registerAddr": 43024}},
                ]
            ],
        },
    )
    client = client_factory()
    batch = await client.async_at_read_batch(SN, [636, 157])
    assert batch == {636: "33", 157: "52"}


async def test_control_sends_value_and_old_value(aioclient_mock: AiohttpClientMocker, client_factory):
    aioclient_mock.post(f"{API_BASE_URL}{RESOURCE_CONTROL}", json={"code": "0", "data": {}})
    client = client_factory()
    assert await client.async_control(SN, 636, "33", old_value="35") is True

    import json as _json

    sent = _json.loads(aioclient_mock.mock_calls[0][2])
    assert sent == {"inverterSn": SN, "cid": "636", "value": "33", "yuanzhi": "35"}


async def test_password_stored_only_as_md5(client_factory):
    client = client_factory(username="u", password="hunter2")
    assert not hasattr(client, "_password")
    assert client._password_md5 == "2ab96390c7dbe3439de74d0c9b0b1767"
