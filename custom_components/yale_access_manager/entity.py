"""Shared native entity identity and dynamic managed-access registration."""

import re

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCK, DOMAIN
from .models import metadata


def setup_access_entities(hass, entry, async_add_entities, domain, factory):
    added = set()
    prefix = f"{DOMAIN}_{entry.data[CONF_LOCK]}_"

    def update():
        keys = set(entry.runtime_data.accesses)
        fresh = keys - added
        if fresh:
            async_add_entities([entity for key in sorted(fresh) for entity in factory(key)])
        added.update(fresh)
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.platform != DOMAIN or entity.domain != domain or not entity.unique_id.startswith(prefix):
                continue
            suffix = entity.unique_id[len(prefix):]
            match = re.fullmatch(r"([0-9a-f]{32})_(?:status|expires|enabled)", suffix)
            if match and match[1] not in keys:
                registry.async_remove(entity.entity_id)
        added.intersection_update(keys)

    update()
    entry.async_on_unload(entry.runtime_data.async_add_listener(update))


class ManagedAccessEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry, access_id, key):
        super().__init__(entry.runtime_data)
        self.access_id = access_id
        self.entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_LOCK]}_{access_id}_{key}"
        self._attr_translation_key = f"access_{key}"
        self._attr_translation_placeholders = {"guest": metadata(entry.runtime_data.accesses[access_id]["metadata"])["name"]}
        self._attr_device_info = {"identifiers": {("august", entry.data[CONF_LOCK])}}

    @property
    def access(self):
        return (self.coordinator.data or {}).get("accesses", {}).get(self.access_id)

    @property
    def available(self):
        return super().available and self.access is not None

    @property
    def extra_state_attributes(self):
        item = self.access or {}
        return {"access_id": self.access_id, "person_entity_id": item.get("person_entity_id"),
                "guest_name": item.get("name"), "notification_status": item.get("notification_status")}
