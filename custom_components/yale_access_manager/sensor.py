"""Only counts are entity states; access names, schedules and PIN stay out."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCK, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AccessCount(entry, key) for key in ("total", "managed", "pending")])


class AccessCount(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lock-smart"

    def __init__(self, entry, key):
        super().__init__(entry.runtime_data)
        self.key = key
        self._attr_unique_id = f"{DOMAIN}_{entry.unique_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = {"identifiers": {("august", entry.data[CONF_LOCK])}}

    @property
    def native_value(self):
        return self.coordinator.data.get(self.key) if self.coordinator.data else None
