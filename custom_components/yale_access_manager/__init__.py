"""Native Yale access management, separate from lock operation."""

from functools import partial

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, service

from .api import create_api
from .const import ACCESS_TYPES, CONF_DEVICE, CONF_LOCK, CONF_SOURCE, DOMAIN, AccessError
from .manager import AccessManager

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
ACCESS_FIELDS = {
    vol.Required("name"): cv.string,
    vol.Required("pin"): cv.string,
    vol.Required("access_type"): vol.In(ACCESS_TYPES),
    vol.Optional("starts_at"): cv.string,
    vol.Optional("ends_at"): cv.string,
    vol.Optional("weekdays"): [cv.string],
    vol.Optional("start_time"): cv.string,
    vol.Optional("end_time"): cv.string,
}


async def handle_action(hass: HomeAssistant, action: str, call: ServiceCall):
    managers = [entry.runtime_data for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
                and entry.data[CONF_DEVICE] == call.data[CONF_DEVICE]]
    if len(managers) != 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")
    manager = managers[0]
    try:
        if action == "list_accesses":
            return await manager.list_accesses()
        if action == "create_access":
            access_id = await manager.create(dict(call.data))
            if call.return_response:
                return {"access_id": access_id, "state": "loaded"}
        elif action == "update_access":
            await manager.update(call.data["access_id"], dict(call.data))
        elif action == "reconcile_access":
            await manager.reconcile(call.data["access_id"], dict(call.data))
        else:
            await manager.delete(call.data["access_id"])
    except AccessError as exc:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key=exc.code) from None
    finally:
        if action != "list_accesses":
            await manager.async_request_refresh()


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    schemas = {
        "list_accesses": {vol.Required(CONF_DEVICE): cv.string},
        "create_access": {vol.Required(CONF_DEVICE): cv.string, **ACCESS_FIELDS},
        "update_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string, **ACCESS_FIELDS},
        "reconcile_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string, **ACCESS_FIELDS},
        "delete_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string},
    }
    for action, schema in schemas.items():
        response = (SupportsResponse.ONLY if action == "list_accesses" else
                    SupportsResponse.OPTIONAL if action == "create_access" else SupportsResponse.NONE)
        service.async_register_admin_service(hass, DOMAIN, action, partial(handle_action, hass, action),
                                             schema=vol.Schema(schema), supports_response=response)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    source = hass.config_entries.async_get_entry(entry.data[CONF_SOURCE])
    device = dr.async_get(hass).async_get(entry.data[CONF_DEVICE])
    if (source is None or source.domain != "august" or source.state is not ConfigEntryState.LOADED
            or device is None or ("august", entry.data[CONF_LOCK]) not in device.identifiers
            or source.entry_id not in device.config_entries):
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="source_unavailable")
    api = None
    try:
        api = await create_api(hass, source)
        manager = AccessManager(hass, entry, api)
        await manager.load()
        await manager.async_config_entry_first_refresh()
    except AccessError as exc:
        if api:
            await api.close()
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key=exc.code) from None
    except Exception:
        if api:
            await api.close()
        raise
    entry.runtime_data = manager
    entry.async_on_unload(manager.close)
    # Add this config entry to the same registered physical device.
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                                          identifiers={("august", entry.data[CONF_LOCK])})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
