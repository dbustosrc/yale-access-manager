"""Native access history without PIN values or changes to person presence."""

from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_LOCK, DOMAIN, EVENT_TYPES


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AccessActivity(entry)])


class AccessActivity(EventEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "access_activity"
    _attr_event_types = list(EVENT_TYPES)
    _attr_icon = "mdi:history"

    def __init__(self, entry):
        self.lock_id = entry.data[CONF_LOCK]
        self._attr_unique_id = f"{DOMAIN}_{self.lock_id}_activity"
        self._attr_device_info = {"identifiers": {("august", self.lock_id)}}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{DOMAIN}_{self.lock_id}_activity", self._receive))

    @callback
    def _receive(self, payload):
        self._trigger_event(payload["event_type"], {key: value for key, value in payload.items() if key != "event_type"})
        self.async_write_ha_state()
