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

PLATFORMS = [Platform.SENSOR, Platform.EVENT, Platform.BINARY_SENSOR, Platform.SWITCH]
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
    device = dr.async_get(hass).async_get(call.data[CONF_DEVICE])
    # HA scopes device IDs per config entry; August and this integration differ.
    managers = [entry.runtime_data for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
                and device is not None
                and ("august", entry.data[CONF_LOCK]) in device.identifiers
                and device.config_entry_id in (entry.entry_id, entry.data[CONF_SOURCE])]
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
        elif action == "bind_person":
            await manager.bind_person(call.data["access_id"], call.data["person_entity_id"], call.data.get("notification_entry_id"), clear_notification=call.data.get("clear_notification", False))
        elif action == "unbind_person":
            await manager.unbind_person(call.data["access_id"])
        elif action in ("disable_access", "enable_access"):
            await manager.set_enabled(call.data["access_id"], action == "enable_access")
        elif action == "issue_access":
            result = await manager.issue_access(call.data["person_entity_id"], validity_minutes=call.data.get("validity_minutes", 10), language=call.data.get("language"))
            if call.return_response:
                return result
        elif action == "begin_visit":
            return await manager.begin_visit(call.data["person_entity_id"], cooldown_minutes=call.data.get("cooldown_minutes", 30))
        elif action == "confirm_delivery":
            return await manager.confirm_delivery(call.data["access_id"], call.data["receipt_action"])
        elif action == "end_visit":
            await manager.end_visit(call.data["person_entity_id"])
        elif action == "delete_access":
            await manager.delete(call.data["access_id"])
        elif action == "cancel_pending_access":
            await manager.delete(call.data["access_id"], cancellation_pin=call.data["pin"])
    except AccessError as exc:
        if action not in ("list_accesses", "begin_visit", "end_visit"):
            await manager.async_request_refresh()
        if exc.detail:
            raise HomeAssistantError(exc.detail) from None
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key=exc.code) from None


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    schemas = {
        "list_accesses": {vol.Required(CONF_DEVICE): cv.string},
        "create_access": {vol.Required(CONF_DEVICE): cv.string, **ACCESS_FIELDS},
        "update_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string, **ACCESS_FIELDS},
        "reconcile_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string, **ACCESS_FIELDS},
        "delete_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string},
        "cancel_pending_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string,
                                  vol.Required("pin"): cv.string},
        "disable_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string},
        "enable_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string},
        "bind_person": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string,
                        vol.Required("person_entity_id"): cv.entity_id, vol.Optional("notification_entry_id"): cv.string,
                        vol.Optional("clear_notification"): cv.boolean},
        "unbind_person": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string},
        "confirm_delivery": {vol.Required(CONF_DEVICE): cv.string, vol.Required("access_id"): cv.string, vol.Required("receipt_action"): cv.string},
        "issue_access": {vol.Required(CONF_DEVICE): cv.string, vol.Required("person_entity_id"): cv.entity_id,
                         vol.Optional("validity_minutes"): vol.All(vol.Coerce(int), vol.Range(min=5, max=30)), vol.Optional("language"): vol.In(("en", "es"))},
        "begin_visit": {vol.Required(CONF_DEVICE): cv.string, vol.Required("person_entity_id"): cv.entity_id,
                        vol.Optional("cooldown_minutes"): vol.All(vol.Coerce(int), vol.Range(min=5, max=120))},
        "end_visit": {vol.Required(CONF_DEVICE): cv.string, vol.Required("person_entity_id"): cv.entity_id},
    }
    for action, schema in schemas.items():
        response = (SupportsResponse.ONLY if action in ("list_accesses", "begin_visit", "confirm_delivery") else
                    SupportsResponse.OPTIONAL if action in ("create_access", "issue_access") else SupportsResponse.NONE)
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
        if exc.detail:
            raise ConfigEntryNotReady(exc.detail) from None
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key=exc.code) from None
    except Exception:
        if api:
            await api.close()
        raise
    entry.runtime_data = manager
    entry.async_on_unload(manager.close)
    # Register this integration's device using the same physical lock identity.
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                                          identifiers={("august", entry.data[CONF_LOCK])})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    manager.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
