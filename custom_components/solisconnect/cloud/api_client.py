"""Async client for the SolisCloud HTTP API.

Ported from the SolisConnect prototype and hardened for Home Assistant use:
- the aiohttp session is injected (use homeassistant.helpers.aiohttp_client.async_get_clientsession)
- requests are serialized with a minimum spacing because the API rate-limits aggressively
- transport failures are retried with backoff; HTTP 408 usually means local clock skew (>15 min)
- the CSRF token from /v2/api/login is optional: reads and control work with HMAC alone,
  but the token is attached when account credentials are configured.

Signing scheme (shared by all SolisCloud API consumers):
  string-to-sign = "POST\n{base64(md5(body))}\napplication/json\n{RFC1123 GMT date}\n{resource}"
  Authorization  = "API {key_id}:{base64(hmac_sha1(key_secret, string-to-sign))}"
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from email.utils import formatdate

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE_URL = "https://www.soliscloud.com:13333"

RESOURCE_LOGIN = "/v2/api/login"
RESOURCE_CONTROL = "/v2/api/control"
RESOURCE_INVERTER_LIST = "/v1/api/inverterList"
RESOURCE_INVERTER_DETAIL = "/v1/api/inverterDetail"
RESOURCE_AT_READ = "/v2/api/atRead"
RESOURCE_AT_READ_BATCH = "/v2/api/atReadBatch"

TOKEN_LIFETIME = timedelta(minutes=30)
REQUEST_TIMEOUT_SECONDS = 10
MIN_REQUEST_SPACING_SECONDS = 1.0
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0


class SolisCloudApiError(Exception):
    """A SolisCloud API request failed."""


class SolisCloudAuthError(SolisCloudApiError):
    """Authentication with the SolisCloud API failed."""


class SolisCloudRateLimitError(SolisCloudApiError):
    """The SolisCloud API rejected the request due to rate limiting."""


class SolisCloudApiClient:
    """Signed, rate-limited client for the SolisCloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        key_id: str,
        key_secret: str,
        username: str | None = None,
        password: str | None = None,
        base_url: str = API_BASE_URL,
    ):
        self._session = session
        self._key_id = key_id
        self._key_secret = key_secret
        self._username = username
        # Only the MD5 of the account password is ever needed or stored.
        self._password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest() if password else None
        self._base_url = base_url

        self._token: str | None = None
        self._token_timestamp = datetime(1970, 1, 1, tzinfo=UTC)
        self._request_lock = asyncio.Lock()
        self._last_request_monotonic = 0.0

    @property
    def has_account_credentials(self) -> bool:
        return bool(self._username and self._password_md5)

    @staticmethod
    def _build_body(params: dict) -> str:
        """Serialize the request body deterministically; the signature covers these exact bytes."""
        return json.dumps(params, separators=(",", ":"))

    @staticmethod
    def _content_md5(body: str) -> str:
        return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")

    def _signed_headers(self, body: str, resource: str, date: str | None = None) -> dict[str, str]:
        content_md5 = self._content_md5(body)
        content_type = "application/json"
        # RFC 1123 date; email.utils.formatdate is locale-independent (strftime("%a") is not).
        if date is None:
            date = formatdate(usegmt=True)

        string_to_sign = f"POST\n{content_md5}\n{content_type}\n{date}\n{resource}"
        digest = hmac.new(self._key_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        signature = base64.b64encode(digest).decode("utf-8")

        return {
            "Content-MD5": content_md5,
            "Content-Type": content_type,
            "Date": date,
            "Authorization": f"API {self._key_id}:{signature}",
        }

    async def _wait_for_request_slot(self):
        """Enforce minimum spacing between requests (the API 502s under bursts)."""
        elapsed = time.monotonic() - self._last_request_monotonic
        if elapsed < MIN_REQUEST_SPACING_SECONDS:
            await asyncio.sleep(MIN_REQUEST_SPACING_SECONDS - elapsed)

    async def _post(self, resource: str, params: dict, *, with_token: bool = False, return_full: bool = False) -> dict:
        """Send a signed POST and return the parsed response 'data' payload (or the full payload)."""
        body = self._build_body(params)
        last_error: Exception | None = None

        async with self._request_lock:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                await self._wait_for_request_slot()
                headers = self._signed_headers(body, resource)
                if with_token and self._token:
                    headers["token"] = self._token
                try:
                    async with self._session.post(
                        f"{self._base_url}{resource}",
                        data=body,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                    ) as response:
                        self._last_request_monotonic = time.monotonic()

                        if response.status == 408:
                            raise SolisCloudApiError(
                                f"SolisCloud rejected the request time on {resource} (HTTP 408): check that the local clock is within 15 minutes of UTC"
                            )
                        if response.status == 429:
                            raise SolisCloudRateLimitError(f"SolisCloud rate limit hit on {resource} (HTTP 429)")
                        response.raise_for_status()

                        payload = await response.json()
                        code = str(payload.get("code", ""))
                        if code != "0":
                            msg = payload.get("msg", "")
                            raise SolisCloudApiError(f"SolisCloud API error on {resource}: code={code} msg={msg}")
                        return payload if return_full else payload.get("data", {})
                except (TimeoutError, aiohttp.ClientError) as error:
                    self._last_request_monotonic = time.monotonic()
                    last_error = error
                    status = getattr(error, "status", None)
                    # Client-side errors (4xx) won't improve on retry; transport failures and 5xx might.
                    if status is not None and 400 <= status < 500:
                        break
                    _LOGGER.debug("SolisCloud request to %s failed (attempt %s/%s): %s", resource, attempt, MAX_ATTEMPTS, error)
                    if attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        raise SolisCloudApiError(f"SolisCloud request to {resource} failed after {MAX_ATTEMPTS} attempts: {last_error}") from last_error

    def _is_token_expired(self) -> bool:
        return self._token is None or datetime.now(UTC) - self._token_timestamp > TOKEN_LIFETIME

    async def async_login(self) -> str:
        """Log in with account credentials and cache the CSRF token."""
        if not self.has_account_credentials:
            raise SolisCloudAuthError("SolisCloud username/password are not configured")
        if not self._is_token_expired():
            return self._token

        payload = await self._post(RESOURCE_LOGIN, {"username": self._username, "password": self._password_md5}, return_full=True)
        # The token key has been observed both at the top level and inside "data",
        # under either name ("csrfToken" or "token").
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = payload.get("csrfToken") or data.get("csrfToken") or payload.get("token") or data.get("token")
        if not token:
            raise SolisCloudAuthError(f"SolisCloud login succeeded but returned no csrfToken (payload keys: {sorted(payload)})")
        self._token = token
        self._token_timestamp = datetime.now(UTC)
        _LOGGER.debug("SolisCloud login successful; token cached for %s", TOKEN_LIFETIME)
        return token

    async def async_ensure_token(self) -> str | None:
        """Refresh the CSRF token if credentials are available; returns None otherwise."""
        if not self.has_account_credentials:
            return None
        return await self.async_login()

    async def async_inverter_list(self, station_id: str) -> list[dict]:
        """List inverters for a plant/station; returns the record dicts (id, sn, ...)."""
        data = await self._post(RESOURCE_INVERTER_LIST, {"stationId": str(station_id)})
        return data.get("page", {}).get("records", [])

    async def async_inverter_detail(self, inverter_sn: str, inverter_id: str | None = None) -> dict:
        """Fetch the full inverterDetail metrics blob for one inverter."""
        params = {"sn": str(inverter_sn)}
        if inverter_id is not None:
            params["id"] = str(inverter_id)
        return await self._post(RESOURCE_INVERTER_DETAIL, params)

    async def async_at_read(self, inverter_sn: str, cid: int) -> str:
        """Read a single control register (CID); returns its value string."""
        await self.async_ensure_token()
        data = await self._post(RESOURCE_AT_READ, {"inverterSn": str(inverter_sn), "cid": str(cid)}, with_token=True)
        if not isinstance(data, dict) or "msg" not in data:
            raise SolisCloudApiError(f"SolisCloud atRead for cid {cid} returned unexpected payload: {data}")
        return str(data["msg"])

    async def async_at_read_batch(self, inverter_sn: str, cids: list[int]) -> dict[int, str]:
        """Read several control registers in one request; returns {cid: value string}."""
        await self.async_ensure_token()
        data = await self._post(
            RESOURCE_AT_READ_BATCH,
            {"inverterSn": str(inverter_sn), "cids": ",".join(str(c) for c in cids)},
            with_token=True,
        )

        # Observed live shape: data is a nested list [[{cid, msg, yuanzhi, sysCommand}, ...]]
        # with string cids; accept flat lists and dict wrappers too.
        if isinstance(data, dict):
            raw = data.get("list") or data.get("records") or []
        else:
            raw = data or []
        records: list[dict] = []
        for item in raw:
            if isinstance(item, list):
                records.extend(r for r in item if isinstance(r, dict))
            elif isinstance(item, dict):
                records.append(item)

        results: dict[int, str] = {}
        for record in records:
            if "cid" in record and "msg" in record:
                try:
                    results[int(record["cid"])] = str(record["msg"])
                except (TypeError, ValueError):
                    continue
        if not results:
            _LOGGER.warning("SolisCloud atReadBatch returned no parseable records; raw payload: %s", data)
        return results

    @staticmethod
    def _control_failure(payload) -> str | None:
        """Return an embedded control failure message from a successful HTTP/API payload."""
        if isinstance(payload, list):
            for item in payload:
                failure = SolisCloudApiClient._control_failure(item)
                if failure:
                    return failure
            return None
        if not isinstance(payload, dict):
            return None

        code = payload.get("code") or payload.get("resultCode") or payload.get("errorCode")
        if code not in (None, "", "0", 0):
            return str(payload.get("msg") or payload.get("message") or f"code={code}")

        for key in ("success", "result", "isSuccess"):
            if payload.get(key) is False:
                return str(payload.get("msg") or payload.get("message") or f"{key}=false")

        for key in ("data", "result", "response", "responses", "records", "list"):
            if key in payload:
                failure = SolisCloudApiClient._control_failure(payload[key])
                if failure:
                    return failure
        return None

    async def async_control(self, inverter_sn: str, cid: int, value: str, old_value: str | None = None) -> bool:
        """Write a control register (CID) value; returns True on success."""
        await self.async_ensure_token()
        params = {"inverterSn": str(inverter_sn), "cid": str(cid), "value": str(value)}
        if old_value is not None:
            params["yuanzhi"] = str(old_value)
        data = await self._post(RESOURCE_CONTROL, params, with_token=True)
        failure = self._control_failure(data)
        if failure:
            raise SolisCloudApiError(f"SolisCloud control for cid {cid} failed: {failure}")
        return True
