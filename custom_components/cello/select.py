"""Per-car zone picker used by the Start parking button."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CelloConfigEntry
from .entity import CelloCarEntity

NO_ZONE = "—"


async def async_setup_entry(
    hass: HomeAssistant, entry: CelloConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        entity
        for car in coordinator.data["cars"]
        for entity in (CelloZoneSelect(coordinator, car), CelloSuggestedZone(coordinator, car))
    )


class CelloZoneSelect(CelloCarEntity, SelectEntity):
    """Where Start parking parks this car (city – zone)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "zone_choice")

    @property
    def options(self) -> list[str]:
        return [NO_ZONE, *self.coordinator.zone_options]

    @property
    def current_option(self) -> str:
        chosen = self.coordinator.prefs(self.car).get("zone")
        if chosen:
            for label, ids in self.coordinator.zone_options.items():
                if list(ids) == list(chosen):
                    return label
        return NO_ZONE

    async def async_select_option(self, option: str) -> None:
        ids = self.coordinator.zone_options.get(option)
        self.coordinator.set_pref(self.car, "zone", list(ids) if ids else None)


PICK = "…"


class CelloSuggestedZone(CelloCarEntity, SelectEntity):
    """Zones where the car is (best first), or the matches of the Zone search.

    Picking one sets the car's Parking zone, so the full list never has to be
    scrolled.
    """

    _attr_icon = "mdi:map-marker-star"

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "zone_suggestion")

    @property
    def options(self) -> list[str]:
        return [PICK, *self.coordinator.locator.options_for(self.car)]

    @property
    def current_option(self) -> str:
        chosen = self.coordinator.prefs(self.car).get("zone")
        if chosen:
            for label in self.coordinator.locator.options_for(self.car):
                if list(self.coordinator.zone_options.get(label) or ()) == list(chosen):
                    return label
        return PICK

    @property
    def extra_state_attributes(self) -> dict:
        locator = self.coordinator.locator
        city = (locator.where.get(self.car) or {}).get("city") or {}
        return {
            "search": locator.search.get(self.car) or None,
            "city": city.get("Name"),
            "count": len(locator.options_for(self.car)),
        }

    async def async_select_option(self, option: str) -> None:
        ids = self.coordinator.zone_options.get(option)
        if ids:
            self.coordinator.set_pref(self.car, "zone", list(ids))
