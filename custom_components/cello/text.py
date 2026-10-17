"""Per-car zone search: what is typed narrows the Suggested zone list."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CelloConfigEntry
from .entity import CelloCarEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: CelloConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(CelloZoneSearch(coordinator, car) for car in coordinator.data["cars"])


class CelloZoneSearch(CelloCarEntity, TextEntity):
    """City or zone words, e.g. "חיפה" or "תל אביב מרכז"; empty = zones where the car is."""

    _attr_native_max = 60
    _attr_icon = "mdi:magnify"

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "zone_search")

    @property
    def native_value(self) -> str:
        return self.coordinator.locator.search.get(self.car, "")

    async def async_set_value(self, value: str) -> None:
        self.coordinator.locator.set_search(self.car, value)
