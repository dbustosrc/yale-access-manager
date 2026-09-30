"""One administrator-controlled switch per managed guest; no owner switch."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_DEVICE, DOMAIN
from .entity import ManagedAccessEntity, setup_access_entities


async def async_setup_entry(hass, entry, async_add_entities):
    setup_access_entities(hass, entry, async_add_entities, "switch", lambda key: [AccessEnabled(entry, key)])


class AccessEnabled(ManagedAccessEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry, access_id):
        super().__init__(entry, access_id, "enabled")

    @property
    def available(self):
        item = self.access or {}
        return super().available and item.get("identity_valid", True) and item.get("state") in ("loaded", "disabled") and item.get("operation") == "ready"

    @property
    def is_on(self):
        return (self.access or {}).get("state") == "loaded"

    async def async_turn_on(self, **kwargs):
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_enabled(False)

    async def _set_enabled(self, enabled):
        # Reuse the existing admin service and caller context rather than duplicate permissions.
        await self.hass.services.async_call(DOMAIN, "enable_access" if enabled else "disable_access",
            {CONF_DEVICE: self.entry.data[CONF_DEVICE], "access_id": self.access_id}, blocking=True, context=self._context)
