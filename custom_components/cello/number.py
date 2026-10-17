"""Per-car "park for" hours used by the Start parking button (0 = open-ended)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CelloConfigEntry
from .entity import CelloCarEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: CelloConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(CelloHoursNumber(coordinator, car) for car in coordinator.data["cars"])


class CelloHoursNumber(CelloCarEntity, NumberEntity):
    """Cello ends the parking by itself after this many hours."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 24
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "park_hours")

    @property
    def native_value(self) -> float:
        return float(self.coordinator.prefs(self.car).get("hours", 0))

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_pref(self.car, "hours", value)
