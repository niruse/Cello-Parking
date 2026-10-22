"""Per-car Start / Stop / Extend parking buttons."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CelloConfigEntry
from .entity import CelloCarEntity

KEYS = ("start_parking", "stop_parking", "extend_parking", "zone_from_location")


async def async_setup_entry(
    hass: HomeAssistant, entry: CelloConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        CelloParkingButton(coordinator, car, key)
        for car in coordinator.data["cars"]
        for key in KEYS
    )


class CelloParkingButton(CelloCarEntity, ButtonEntity):
    """Start uses the car's Zone and Park-for choices."""

    async def async_press(self) -> None:
        key = self._attr_translation_key
        user_id = self._context.user_id if self._context else None
        if key == "start_parking":
            await self.coordinator.async_start(self.car, user_id=user_id)
        elif key == "stop_parking":
            await self.coordinator.async_stop(self.car)
        elif key == "zone_from_location":
            await self.coordinator.locator.zone_from_location(self.car, user_id)
        else:
            await self.coordinator.async_extend(self.car)
