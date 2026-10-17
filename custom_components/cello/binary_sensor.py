"""Parking-active binary sensor per car."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CelloConfigEntry
from .entity import CelloCarEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: CelloConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        entity
        for car in coordinator.data["cars"]
        for entity in (CelloParkingBinarySensor(coordinator, car), CelloMovedBinarySensor(coordinator, car))
    )


class CelloParkingBinarySensor(CelloCarEntity, BinarySensorEntity):
    """On while a parking session runs for the car."""

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "parking")

    @property
    def is_on(self) -> bool:
        return self.session is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.session or {}
        return {
            "transaction_id": s.get("ID"),
            "city_id": s.get("CityID"),
            "zone_id": s.get("ZoneID"),
            "zone": s.get("ZoneName"),
        }


class CelloMovedBinarySensor(CelloCarEntity, BinarySensorEntity):
    """On when the car drove away while its parking is still running."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "moved_while_parked")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.session_marks(self.car) or {}).get("moved"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        where = self.coordinator.locator.where.get(self.car) or {}
        marks = self.coordinator.session_marks(self.car) or {}
        return {
            "reason": marks.get("moved"),
            "distance_from_parking_m": where.get("distance_m"),
            "source": where.get("source"),
        }
