"""Stock Home Assistant setup and native access-management forms."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigEntryState, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, selector

from .api import create_api
from .const import ACCESS_TYPES, CONF_DEVICE, CONF_LOCK, CONF_SOURCE, DAYS, DOMAIN, AccessError


class YaleAccessManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        sources = [entry for entry in self.hass.config_entries.async_entries("august")
                   if entry.state is ConfigEntryState.LOADED]
        if not sources:
            return self.async_abort(reason="no_august")
        errors = {}
        placeholders = {}
        if user_input is not None:
            device = dr.async_get(self.hass).async_get(user_input[CONF_DEVICE])
            source = self.hass.config_entries.async_get_entry(user_input[CONF_SOURCE])
            ids = [value for domain, value in device.identifiers if domain == "august"] if device else []
            if (source not in sources or device is None or source.entry_id not in device.config_entries or len(ids) != 1):
                errors["base"] = "unknown_device"
            else:
                await self.async_set_unique_id(ids[0])
                self._abort_if_unique_id_configured()
                api = None
                try:
                    api = await create_api(self.hass, source)
                    locks, detail = await api.locks(), await api.detail(ids[0])
                    if ids[0] not in locks or locks[ids[0]].get("UserType") != "superuser":
                        raise AccessError("access_denied")
                    if not detail.get("supportsEntryCodes"):
                        raise AccessError("unsupported_lock")
                    return self.async_create_entry(title=device.name_by_user or device.name or "Yale lock",
                                                    data={CONF_DEVICE: device.id, CONF_SOURCE: source.entry_id, CONF_LOCK: ids[0]})
                except AccessError as exc:
                    errors["base"] = "yale_error" if exc.detail else exc.code
                    placeholders = {"error": exc.detail} if exc.detail else {}
                finally:
                    if api:
                        await api.close()
        return self.async_show_form(step_id="user", errors=errors, description_placeholders=placeholders, data_schema=vol.Schema({
            vol.Required(CONF_SOURCE): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": source.entry_id, "label": source.title} for source in sources],
                mode=selector.SelectSelectorMode.DROPDOWN)),
            vol.Required(CONF_DEVICE): selector.DeviceSelector(selector.DeviceSelectorConfig(integration="august")),
        }))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AccessOptionsFlow()


class AccessOptionsFlow(OptionsFlow):
    """Manage credentials through native forms; never save a PIN in options."""

    def __init__(self):
        self._draft = {}
        self._mode = "create_access"
        self._access_id = None

    @property
    def manager(self):
        return self.config_entry.runtime_data

    async def async_step_init(self, user_input=None):
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_ready")
        return self.async_show_menu(step_id="init", menu_options=["list_accesses", "create_access", "update_access", "bind_person",
                                                                 "disable_access", "enable_access", "reconcile_access", "delete_access"])

    async def async_step_list_accesses(self, user_input=None):
        if user_input is not None:
            return await self.async_step_init()
        try:
            listing = await self.manager.list_accesses()
        except AccessError as exc:
            if exc.detail:
                return self.async_abort(reason="yale_error", description_placeholders={"error": exc.detail})
            return self.async_abort(reason="cannot_connect")
        lines = [f"{item['name']} | {item['access_type']} | {item['state']} | "
                 f"{item['operation']} | {item['access_id']} | {item.get('person_entity_id') or '—'}" for item in listing["accesses"]]
        # Fenced plain text keeps guest names from becoming Markdown links.
        content = "```text\n" + "\n".join(lines).replace("`", "") + "\n```" if lines else "—"
        return self.async_show_form(step_id="list_accesses", data_schema=vol.Schema({}),
                                    description_placeholders={"accesses": content})

    async def async_step_create_access(self, user_input=None):
        self._mode, self._access_id = "create_access", None
        return await self._details(user_input)

    async def _select(self, step_id, user_input):
        errors = {}
        placeholders = {}
        if user_input is not None:
            self._access_id = user_input["access_id"]
            if step_id in ("update_access", "reconcile_access"):
                self._mode = step_id
                return await self._details()
            if step_id == "bind_person":
                return await self.async_step_person()
            try:
                if step_id == "delete_access":
                    await self.manager.delete(self._access_id)
                else:
                    await self.manager.set_enabled(self._access_id, step_id == "enable_access")
                return self.async_create_entry(title="", data={})
            except AccessError as exc:
                errors["base"] = "yale_error" if exc.detail else exc.code
                placeholders = {"error": exc.detail} if exc.detail else {}
        try:
            listing = await self.manager.list_accesses()
        except AccessError as exc:
            if exc.detail:
                return self.async_abort(reason="yale_error", description_placeholders={"error": exc.detail})
            return self.async_abort(reason="cannot_connect")
        choices = [{"value": item["access_id"], "label": f"{item['name']} · {item['state']} · {item['operation']}"}
                   for item in listing["accesses"] if item["managed"]]
        if not choices:
            return self.async_abort(reason="no_managed_accesses")
        return self.async_show_form(step_id=step_id, errors=errors, description_placeholders=placeholders, data_schema=vol.Schema({
            vol.Required("access_id"): selector.SelectSelector(selector.SelectSelectorConfig(options=choices))}))

    async def async_step_update_access(self, user_input=None):
        return await self._select("update_access", user_input)

    async def async_step_delete_access(self, user_input=None):
        return await self._select("delete_access", user_input)

    async def async_step_reconcile_access(self, user_input=None):
        return await self._select("reconcile_access", user_input)

    async def async_step_disable_access(self, user_input=None):
        return await self._select("disable_access", user_input)

    async def async_step_enable_access(self, user_input=None):
        return await self._select("enable_access", user_input)

    async def async_step_bind_person(self, user_input=None):
        return await self._select("bind_person", user_input)

    async def async_step_person(self, user_input=None):
        errors = {}
        placeholders = {}
        if user_input is not None:
            try:
                await self.manager.bind_person(self._access_id, user_input["person_entity_id"], user_input["notification_entry_id"])
                return self.async_create_entry(title="", data={})
            except AccessError as exc:
                errors["base"] = "yale_error" if exc.detail else exc.code
                placeholders = {"error": exc.detail} if exc.detail else {}
        phones = [{"value": entry.entry_id, "label": entry.title} for entry in self.hass.config_entries.async_entries("mobile_app")
                  if entry.state is ConfigEntryState.LOADED]
        if not phones:
            return self.async_abort(reason="no_mobile_app")
        return self.async_show_form(step_id="person", errors=errors, description_placeholders=placeholders, data_schema=vol.Schema({
            vol.Required("person_entity_id"): selector.EntitySelector(selector.EntitySelectorConfig(filter={"domain": "person"})),
            vol.Required("notification_entry_id"): selector.SelectSelector(selector.SelectSelectorConfig(options=phones)),
        }))

    async def _details(self, user_input=None, errors=None, detail=None):
        if user_input is not None:
            self._draft = dict(user_input)
            if self._draft.get("access_type") not in ACCESS_TYPES:
                return await self._details(errors={"base": "invalid_schedule"})
            if self._draft["access_type"] == "always":
                return await self._apply("details")
            return await self.async_step_schedule()
        return self.async_show_form(step_id="details", errors=errors or {},
                                   description_placeholders={"error": detail} if detail else {}, data_schema=vol.Schema({
            vol.Required("name"): selector.TextSelector(),
            vol.Required("pin"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
            vol.Required("access_type", default="temporary"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=list(ACCESS_TYPES), translation_key="access_type")),
        }))

    async def async_step_details(self, user_input=None):
        return await self._details(user_input)

    async def async_step_schedule(self, user_input=None, errors=None, detail=None):
        if user_input is not None:
            self._draft.update(user_input)
            return await self._apply("schedule")
        if self._draft["access_type"] == "temporary":
            fields = {vol.Required("starts_at"): selector.DateTimeSelector(),
                      vol.Required("ends_at"): selector.DateTimeSelector()}
        else:
            fields = {vol.Required("weekdays"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=list(DAYS), multiple=True, translation_key="weekdays")),
                      vol.Required("start_time"): selector.TimeSelector(),
                      vol.Required("end_time"): selector.TimeSelector()}
        return self.async_show_form(step_id="schedule", errors=errors or {},
                                   description_placeholders={"error": detail} if detail else {}, data_schema=vol.Schema(fields))

    async def _apply(self, step_id):
        if self.config_entry.state is not ConfigEntryState.LOADED:
            self._draft.clear()
            return self.async_abort(reason="not_ready")
        try:
            if self._mode == "create_access":
                await self.manager.create(self._draft)
            elif self._mode == "update_access":
                await self.manager.update(self._access_id, self._draft)
            else:
                await self.manager.reconcile(self._access_id, self._draft)
        except AccessError as exc:
            errors = {"base": "yale_error" if exc.detail else exc.code}
            if step_id == "schedule":
                return await self.async_step_schedule(errors=errors, detail=exc.detail)
            return await self._details(errors=errors, detail=exc.detail)
        finally:
            await self.manager.async_request_refresh()
        self._draft.clear()
        return self.async_create_entry(title="", data={})
