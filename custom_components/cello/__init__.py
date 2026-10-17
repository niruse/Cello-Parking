"""The Cello integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import CelloApiClient
from .const import (
    CONF_ACCOUNT_KEY,
    CONF_APP_KEY,
    CONF_BASE_URL,
    CONF_CUSTOMER_ID,
    CONF_TOKEN,
    DOMAIN,
)
from .coordinator import CelloCoordinator
from .location import CarLocator
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

CelloConfigEntry = ConfigEntry[CelloCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Cello actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CelloConfigEntry) -> bool:
    """Set up Cello from a config entry."""
    client = CelloApiClient(
        async_get_clientsession(hass),
        base_url=entry.data.get(CONF_BASE_URL),
        app_key=entry.data.get(CONF_APP_KEY),
        token=entry.data[CONF_TOKEN],
        account_key=entry.data.get(CONF_ACCOUNT_KEY),
    )
    coordinator = CelloCoordinator(hass, entry, client)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.locator = CarLocator(hass, coordinator)
    # The account device first, so each car device can point at it.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_CUSTOMER_ID])},
        name=entry.title,
        manufacturer="Cello",
        model="Parking account",
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.locator.async_start()
    entry.async_on_unload(coordinator.locator.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: CelloConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CelloConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
