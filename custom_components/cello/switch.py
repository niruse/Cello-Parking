"""Switches: per-car auto-renew (done by Home Assistant) and Cello's own
account services (auto-extend / auto-stop, done by Cello's servers)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CelloConfigEntry
from .entity import CelloAccountEntity, CelloCarEntity

# translation key -> Cello setting name
ACCOUNT_SERVICES = {
    "cello_auto_extend": "AutoExtensionPark",
    "cello_auto_stop": "AutoClosePark",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: CelloConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        CelloServiceSwitch(coordinator, key, name) for key, name in ACCOUNT_SERVICES.items()
    ]
    entities.extend(CelloAutoRenewSwitch(coordinator, car) for car in coordinator.data["cars"])
    async_add_entities(entities)


class CelloAutoRenewSwitch(CelloCarEntity, SwitchEntity):
    """Extend the running parking a few minutes before Cello would end it."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "auto_renew")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.prefs(self.car).get("auto_renew"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_pref(self.car, "auto_renew", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_pref(self.car, "auto_renew", False)


class CelloServiceSwitch(CelloAccountEntity, SwitchEntity):
    """One of Cello's account services, as the app's settings show it."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key: str, setting: str) -> None:
        super().__init__(coordinator, key)
        self._setting = setting

    @property
    def is_on(self) -> bool | None:
        settings = self.coordinator.data["status"].get("Settings") or {}
        value = settings.get(self._setting)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_service(self._setting, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_service(self._setting, False)
