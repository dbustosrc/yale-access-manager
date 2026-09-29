"""Direct PIN transport using yalexs branding and HA's existing OAuth session."""

from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import OAuth2TokenRequestReauthError, OAuth2TokenRequestError
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow
from yalexs.api_common import ApiCommon, api_auth_headers
from yalexs.const import Brand

from .const import AccessError


class YaleAPI:
    """Never log raw headers, bodies, URLs or exceptions from the transport."""

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
        # A write is sent once. An ambiguous response must be reconciled, not replayed.
        try:
            async with self.session.request(method, self.urls.get_brand_url(path), headers=headers,
                                            json=payload, timeout=ClientTimeout(total=25),
                                            allow_redirects=False) as response:
                status = response.status
                if status == 401:
                    self.source.async_start_reauth(self.hass)
                    raise AccessError("source_auth")
                if status == 403:
                    raise AccessError("access_denied")
                if status == 429:
                    raise AccessError("rate_limited")
                if status not in (200, 201, 202, 204):
                    raise AccessError("write_rejected" if method == "POST" else "cannot_connect",
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
        except (ClientError, TimeoutError, RuntimeError, ValueError, TypeError):
            raise AccessError("outcome_unknown" if method == "POST" else "cannot_connect",
                              uncertain=method == "POST") from None
        finally:
            headers.clear()

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
