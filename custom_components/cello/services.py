"""Cello actions: start, stop and extend parking, and find zones near a spot."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ACCEPT,
    ATTR_CAR,
    ATTR_CITY,
    ATTR_HOURS,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_UNTIL,
    ATTR_ZONE,
    DOMAIN,
)
from .coordinator import CelloCoordinator, zone_label

CAR_SCHEMA = vol.Schema({vol.Required(ATTR_CAR): cv.string}, extra=vol.ALLOW_EXTRA)

START_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CAR): cv.string,
        vol.Optional(ATTR_CITY): vol.Coerce(int),
        vol.Optional(ATTR_ZONE): vol.Coerce(int),
        vol.Exclusive(ATTR_HOURS, "end"): vol.All(vol.Coerce(float), vol.Range(min=0, max=72)),
        vol.Exclusive(ATTR_UNTIL, "end"): cv.datetime,
        vol.Optional(ATTR_ACCEPT, default=False): cv.boolean,
    }
)
EXTEND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CAR): cv.string,
        vol.Optional(ATTR_ACCEPT, default=False): cv.boolean,
    }
)
FIND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_LATITUDE): cv.latitude,
        vol.Required(ATTR_LONGITUDE): cv.longitude,
    }
)


def _coordinators(hass: HomeAssistant) -> list[CelloCoordinator]:
    return [
        e.runtime_data
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]


def _for_car(hass: HomeAssistant, car: str) -> tuple[CelloCoordinator, str]:
    plate = "".join(ch for ch in car if ch.isdigit())
    for coordinator in _coordinators(hass):
        if plate in (coordinator.data or {}).get("cars", {}):
            return coordinator, plate
    raise ServiceValidationError(f"No Cello account has car {car}")


def _result(body: dict[str, Any]) -> dict[str, Any]:
    session = body.get("Session") or {}
    return {
        "status": body.get("StatusPark"),
        "message": body.get("Message"),
        "transaction_id": session.get("ID"),
        "zone": session.get("ZoneName"),
        "started": session.get("DateIn"),
        "ends": session.get("DateOut"),
        "cost": session.get("Amount"),
    }


def async_setup_services(hass: HomeAssistant) -> None:
    async def start(call: ServiceCall) -> ServiceResponse:
        coordinator, car = _for_car(hass, call.data[ATTR_CAR])
        body = await coordinator.async_start(
            car,
            city=call.data.get(ATTR_CITY),
            zone=call.data.get(ATTR_ZONE),
            hours=call.data.get(ATTR_HOURS),
            until=call.data.get(ATTR_UNTIL),
            accept_message=call.data[ATTR_ACCEPT],
        )
        return _result(body)

    async def stop(call: ServiceCall) -> ServiceResponse:
        coordinator, car = _for_car(hass, call.data[ATTR_CAR])
        return _result(await coordinator.async_stop(car))

    async def extend(call: ServiceCall) -> ServiceResponse:
        coordinator, car = _for_car(hass, call.data[ATTR_CAR])
        return _result(await coordinator.async_extend(car, call.data[ATTR_ACCEPT]))

    async def find_zones(call: ServiceCall) -> ServiceResponse:
        coordinators = _coordinators(hass)
        if not coordinators:
            raise ServiceValidationError("No Cello account is loaded")
        coordinator = coordinators[0]
        cities = await coordinator.client.get_locations_near(
            call.data[ATTR_LATITUDE], call.data[ATTR_LONGITUDE]
        )
        # The location call names the city only; its zones come from the list.
        known = {c.get("ID"): c for c in coordinator.cities}
        cities = [
            c if c.get("Zones") else {**c, "Zones": (known.get(c.get("ID")) or {}).get("Zones") or []}
            for c in cities
        ]
        return {
            "cities": [
                {
                    "city": c.get("ID"),
                    "name": c.get("Name"),
                    "zones": [
                        {"zone": z.get("ID"), "name": z.get("Name"), "label": zone_label(c, z)}
                        for z in c.get("Zones") or []
                    ],
                }
                for c in cities
            ]
        }

    hass.services.async_register(
        DOMAIN, "start_parking", start, START_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "stop_parking", stop, CAR_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "extend_parking", extend, EXTEND_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "find_zones", find_zones, FIND_SCHEMA, supports_response=SupportsResponse.ONLY
    )
