"""Base entities for Cello Parking: one device per account, one per car."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_CUSTOMER_ID, DOMAIN
from .coordinator import CelloCoordinator


class CelloAccountEntity(CoordinatorEntity[CelloCoordinator]):
    """Entity on the account device."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: CelloCoordinator, key: str) -> None:
        super().__init__(coordinator)
        customer = coordinator.config_entry.data[CONF_CUSTOMER_ID]
        self._attr_unique_id = f"{customer}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, customer)},
            name=coordinator.config_entry.title,
            manufacturer="Cello",
            model="Parking account",
        )


class CelloCarEntity(CoordinatorEntity[CelloCoordinator]):
    """Entity on one car's device."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: CelloCoordinator, car: str, key: str) -> None:
        super().__init__(coordinator)
        self.car = car
        customer = coordinator.config_entry.data[CONF_CUSTOMER_ID]
        info = self.car_info
        self._attr_unique_id = f"{customer}_{car}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{customer}_{car}")},
            name=info.get("Nickname") or f"Cello {car}",
            manufacturer="Cello",
            model="Car",
            serial_number=car,
            via_device=(DOMAIN, customer),
        )

    @property
    def car_info(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("cars", {}).get(self.car, {})

    @property
    def session(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get("active", {}).get(self.car)

    @property
    def available(self) -> bool:
        return super().available and self.car in (self.coordinator.data or {}).get(
            "cars", {}
        )
