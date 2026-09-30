"""Native diagnostic alert for unresolved access and connectivity issues."""

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCK, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AccessProblem(entry)])


class AccessProblem(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "access_problem"

    def __init__(self, entry):
        super().__init__(entry.runtime_data)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_LOCK]}_problem"
        self._attr_device_info = {"identifiers": {("august", entry.data[CONF_LOCK])}}

    @property
    def available(self):
        return True

    @property
    def is_on(self):
        return self.coordinator.problem or bool(self.coordinator.last_error)
