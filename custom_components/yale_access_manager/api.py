"""Direct PIN transport using yalexs branding and HA's existing OAuth session."""

import json
import logging
import re
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import OAuth2TokenRequestReauthError, OAuth2TokenRequestError
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow
from yalexs.api_common import ApiCommon, api_auth_headers
from yalexs.const import Brand

from .const import AccessError

_LOGGER = logging.getLogger(__name__)


def redact_error(text: str, secrets: list[str]) -> str:
    """Keep Yale's error wording and structure while removing credentials."""
    def clean(value):
        if isinstance(value, dict):
            return {key: "[redacted]" if re.search(r"pin|token|password|secret|authorization|cookie|api.?key", key, re.I)
                    else clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    try:
        text = json.dumps(clean(json.loads(text)), ensure_ascii=False)
    except ValueError:
        # Text/HTML errors can also echo fields outside a JSON object.
        text = re.sub(r"(?i)((?:pin|token|password|secret|authorization|api.?key)\s*[:=]\s*)[^\s,<>]+",
                      r"\1[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"<>]+", r"\1[redacted]", text)
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    # Yale can embed an unrelated PIN inside an error message.
    return re.sub(r"(?<![\w])\d{4,8}(?![\w])", "[redacted]", text)


class YaleAPI:
    """Log sanitized error responses; never log successful PIN responses."""

    def __init__(self, session: ClientSession, oauth, source, hass) -> None:
        self.session, self.oauth, self.source = session, oauth, source
        self.hass = hass
        self.brand = Brand.YALE_AUGUST
        self.urls = ApiCommon(self.brand)

    async def request(self, method: str, path: str, payload: dict | None = None, *, version="0.0.1"):
        if (self.hass.config_entries.async_get_entry(self.source.entry_id) is not self.source
                or self.source.state is not ConfigEntryState.LOADED):
            raise AccessError("source_unavailable")
        try:
            await self.oauth.async_ensure_token_valid()
        except OAuth2TokenRequestReauthError:
            self.source.async_start_reauth(self.hass)
            raise AccessError("source_auth") from None
        except (OAuth2TokenRequestError, ClientError, TimeoutError):
            raise AccessError("cannot_connect") from None
        token = self.oauth.token.get("access_token")
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise AccessError("source_auth")
        headers = api_auth_headers(token, self.brand)
        headers.update({"Accept-Version": version, "Content-Type": "application/json"})
        secrets = [value for key, value in headers.items() if isinstance(value, str)
                   and re.search(r"authorization|token|api.?key", key, re.I)]
        secrets.extend(value for key, value in self.oauth.token.items()
                       if isinstance(value, str) and ("token" in key or "secret" in key))
        secrets.extend(item.get("pin", "") for item in (payload or {}).get("commands", []) if isinstance(item, dict))
        # A write is sent once. An ambiguous response must be reconciled, not replayed.
        try:
            async with self.session.request(method, self.urls.get_brand_url(path), headers=headers,
                                            json=payload, timeout=ClientTimeout(total=25),
                                            allow_redirects=False) as response:
                status = response.status
                if status not in (200, 201, 202, 204):
                    try:
                        body = redact_error(await response.text(), secrets)
                    except (ClientError, TimeoutError, UnicodeError):
                        body = "[response body unavailable]"
                    detail = f"Yale HTTP {status}: {body}" if body else f"Yale HTTP {status} (empty response)"
                    endpoint = re.sub(r"/(locks|houses)/[^/?]+", r"/\1/{id}", path)
                    _LOGGER.error("Yale %s %s: %s", method, endpoint, detail)
                    if status == 401:
                        self.source.async_start_reauth(self.hass)
                    code = {401: "source_auth", 403: "access_denied", 429: "rate_limited"}.get(
                        status, "write_rejected" if method == "POST" else "cannot_connect")
                    raise AccessError(code, detail=detail,
                                      uncertain=method == "POST" and (status >= 500 or status == 408 or status < 400))
                if status == 204:
                    return {}
                try:
                    result = await response.json()
                except (ValueError, ClientError):
                    raise AccessError("invalid_response", uncertain=method == "POST") from None
                if not isinstance(result, dict):
                    raise AccessError("invalid_response", uncertain=method == "POST")
                return result
        except (ClientError, TimeoutError, RuntimeError, ValueError, TypeError) as exc:
            detail = f"Yale transport {type(exc).__name__}: {redact_error(str(exc), secrets)}"
            _LOGGER.error("Yale %s request failed: %s", method, detail)
            raise AccessError("outcome_unknown" if method == "POST" else "cannot_connect",
                              uncertain=method == "POST", detail=detail) from None
        finally:
            headers.clear()
            secrets.clear()

    async def locks(self) -> dict:
        return await self.request("GET", "/users/locks/mine")

    async def detail(self, lock_id: str) -> dict:
        return await self.request("GET", f"/locks/{quote(lock_id, safe='')}")

    async def pins(self, lock_id: str) -> dict:
        return await self.request("GET", f"/locks/{quote(lock_id, safe='')}/pins")

    async def activities(self, house_id: str) -> list[dict]:
        result = await self.request("GET", f"/houses/{quote(house_id, safe='')}/activities?limit=50", version="4.0.0")
        events = result.get("events")
        if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
            raise AccessError("invalid_response")
        return events

    async def write(self, lock_id: str, command: dict) -> dict:
        return await self.request("POST", f"/locks/{quote(lock_id, safe='')}/pins", {"commands": [command]})

    async def close(self) -> None:
        await self.session.close()


async def create_api(hass, source) -> YaleAPI:
    """Only the source August entry stores and refreshes account credentials."""
    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, source)
    except config_entry_oauth2_flow.ImplementationUnavailableError:
        raise AccessError("source_unavailable") from None
    oauth = config_entry_oauth2_flow.OAuth2Session(hass, source, implementation)
    return YaleAPI(aiohttp_client.async_create_clientsession(hass), oauth, source, hass)
