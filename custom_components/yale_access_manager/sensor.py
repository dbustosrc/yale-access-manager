"""Only counts are entity states; access names, schedules and PIN stay out."""

from datetime import datetime

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCK, DOMAIN
from .entity import ManagedAccessEntity, setup_access_entities


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AccessCount(entry, key) for key in ("total", "managed", "pending")])
    async_add_entities([LastSync(entry)])
    setup_access_entities(hass, entry, async_add_entities, "sensor", lambda key: [AccessState(entry, key), AccessExpiry(entry, key)])


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


class LastSync(AccessCount):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry):
        super().__init__(entry, "last_sync")

    @property
    def available(self):
        return True  # The last successful synchronization remains useful during an outage.

    @property
    def native_value(self):
        return self.coordinator.last_sync


class AccessState(ManagedAccessEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["loaded", "disabled", "pending", "not_found", "identity_changed"]

    def __init__(self, entry, access_id):
        super().__init__(entry, access_id, "status")

    @property
    def native_value(self):
        item = self.access
        if item is None:
            return None
        if not item.get("identity_valid", True):
            return "identity_changed"
        if item["operation"] != "ready":
            return "pending"
        return item["state"] if item["state"] in self._attr_options else "pending"


class AccessExpiry(ManagedAccessEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry, access_id):
        super().__init__(entry, access_id, "expires")

    @property
    def native_value(self):
        item = self.access or {}
        value = item.get("expires_at")
        if not value and item.get("access_type") == "temporary":
            fields = dict(part.split("=", 1) for part in (item.get("schedule") or "").split(";") if "=" in part)
            value = fields.get("DTEND")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result if result.tzinfo else None
        except (AttributeError, TypeError, ValueError):
            return None
