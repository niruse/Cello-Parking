"""Polling coordinator for one Cello account.

Every poll reads the account status, cars and running parkings. Slower data
(zones, this month's parkings, invoices, payment card) is refreshed on its own
clock. The coordinator also owns each car's choices (zone, park-for hours,
auto-renew), and the two things that act on a running parking: auto-renew
(extend shortly before Cello's end time) and reminders.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CelloApiClient,
    CelloAuthError,
    CelloConfirmationNeeded,
    CelloError,
    parse_date,
)
from .const import (
    CONF_ACCOUNT_KEY,
    CONF_APP_KEY,
    CONF_BASE_URL,
    CONF_CUSTOMER_ID,
    CONF_REMINDER_MINUTES,
    CONF_RENEW_BEFORE_MINUTES,
    CONF_SCAN_INTERVAL,
    DEFAULT_REMINDER_MINUTES,
    DEFAULT_RENEW_BEFORE_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_PARKING,
    TIME_ZONE,
)

_LOGGER = logging.getLogger(__name__)

TZ = ZoneInfo(TIME_ZONE)
SLOW_REFRESH = timedelta(minutes=30)
ZONES_REFRESH = timedelta(hours=24)
CONFIRM_WINDOW = timedelta(minutes=3)
STORE_VERSION = 1


def local_time(value: str | None) -> datetime | None:
    """A Cello ``dd-MM-yyyy HH:mm:ss`` time as an aware Israel time."""
    parsed = parse_date(value)
    return parsed.replace(tzinfo=TZ) if parsed else None


def zone_label(city: dict[str, Any], zone: dict[str, Any]) -> str:
    """Unique, readable option text for one zone."""
    return f"{city.get('Name', '').strip()} – {zone.get('Name', '').strip()} [{zone['ID']}]"


class CelloCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Account status, cars and running parkings, plus the car controls."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: CelloApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.data[CONF_CUSTOMER_ID]}"
        )
        # plate -> {"zone": [city_id, zone_id], "hours": float, "auto_renew": bool}
        self.car_prefs: dict[str, dict[str, Any]] = {}
        # session id -> things already done for it (reminded / renewed at)
        self._session_marks: dict[str, dict[str, Any]] = {}
        # Spots where Cello named a city: [lat, lng, city_id], newest last.
        self.city_hits: list[list[float]] = []
        # plate -> (confirm code, message, asked at)
        self._pending_confirm: dict[str, tuple[str, str, datetime]] = {}
        self.cities: list[dict[str, Any]] = []
        self.zone_options: dict[str, tuple[int, int]] = {}
        self._slow: dict[str, Any] = {}
        self._slow_at: datetime | None = None
        self._zones_at: datetime | None = None
        self.locator: Any = None  # CarLocator, set up with the entry

    # --- persistence ----------------------------------------------------

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self.car_prefs = stored.get("cars", {})
        self._session_marks = stored.get("sessions", {})
        self.city_hits = stored.get("city_hits", [])

    def save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "cars": self.car_prefs,
                "sessions": self._session_marks,
                "city_hits": self.city_hits,
            },
            2,
        )

    def prefs(self, car: str) -> dict[str, Any]:
        return self.car_prefs.setdefault(car, {})

    def set_pref(self, car: str, key: str, value: Any) -> None:
        self.prefs(car)[key] = value
        self.save()
        self.async_update_listeners()

    # --- polling --------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        try:
            status = await self.client.get_status()
            cars = await self.client.get_cars()
            active = await self.client.get_active_sessions()
            if not self._zones_at or now - self._zones_at > ZONES_REFRESH:
                await self._refresh_zones()
                self._zones_at = now
            if not self._slow_at or now - self._slow_at > SLOW_REFRESH:
                await self._refresh_slow()
                self._slow_at = now
        except CelloAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CelloError as err:
            raise UpdateFailed(str(err)) from err
        self._persist_client_state()

        by_car: dict[str, dict[str, Any]] = {}
        for park in active:
            if park.get("CarNumber") and not park.get("Close"):
                by_car[str(park["CarNumber"])] = park
        previous = (self.data or {}).get("active", {})
        data = {
            "status": status,
            "cars": {str(c["Number"]): c for c in cars if c.get("Number")},
            "active": by_car,
            **self._slow,
        }
        self._announce_changes(previous, by_car)
        if any(by_car.get(car) is None for car in previous):
            # A parking just ended: its cost is now in this month's list.
            self._slow_at = None
        await self._handle_running(by_car)
        return data

    async def _refresh_zones(self) -> None:
        self.cities = await self.client.get_locations()
        options: dict[str, tuple[int, int]] = {}
        for city in sorted(self.cities, key=lambda c: c.get("Name") or ""):
            for zone in city.get("Zones") or []:
                options[zone_label(city, zone)] = (int(city["ID"]), int(zone["ID"]))
        self.zone_options = options

    async def _refresh_slow(self) -> None:
        today = dt_util.now(TZ)
        month = await self.client.get_transactions(today.year, today.month)
        try:
            invoices = await self.client.get_invoices(today.year)
        except CelloError as err:
            _LOGGER.debug("Invoices not read: %s", err)
            invoices = self._slow.get("invoices", [])
        try:
            payment = await self.client.get_payment_method()
        except CelloError as err:
            _LOGGER.debug("Payment method not read: %s", err)
            payment = self._slow.get("payment", {})
        self._slow = {
            "month": month.get("List") or [],
            "month_pdf": month.get("PDF"),
            "invoices": invoices,
            "payment": payment,
        }

    def _persist_client_state(self) -> None:
        """Keep a re-registered App-Key / new domain across restarts."""
        entry = self.config_entry
        new = {
            CONF_APP_KEY: self.client.app_key,
            CONF_BASE_URL: self.client.base_url,
            CONF_ACCOUNT_KEY: self.client.account_key,
        }
        if any(entry.data.get(k) != v for k, v in new.items()):
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **new}
            )

    # --- events, reminders, auto-renew -------------------------------------

    def session_marks(self, car: str) -> dict[str, Any] | None:
        """Per-parking memory of the car's running parking (None when not parked)."""
        session = (self.data or {}).get("active", {}).get(car)
        if not session:
            return None
        return self._session_marks.setdefault(str(session.get("ID")), {})

    def fire(self, kind: str, car: str, session: dict[str, Any] | None, **extra: Any) -> None:
        self._fire(kind, car, session, **extra)

    def _fire(self, kind: str, car: str, session: dict[str, Any] | None, **extra: Any) -> None:
        s = session or {}
        self.hass.bus.async_fire(
            EVENT_PARKING,
            {
                "type": kind,
                "car": car,
                "zone": s.get("ZoneName"),
                "transaction_id": s.get("ID"),
                "cost": s.get("Amount"),
                **extra,
            },
        )

    def _announce_changes(
        self, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
    ) -> None:
        if self.data is None:
            return  # first poll after start-up: nothing "changed"
        for car, session in after.items():
            if car not in before:
                self._fire("started", car, session)
                # Habit, for ranking zone suggestions: where this car parks.
                if session.get("ZoneID"):
                    counts = self.prefs(car).setdefault("zone_counts", {})
                    zone = str(session["ZoneID"])
                    counts[zone] = counts.get(zone, 0) + 1
                    self.save()
        for car, session in before.items():
            if car not in after:
                self._fire("ended", car, session)

    async def _handle_running(self, active: dict[str, dict[str, Any]]) -> None:
        options = self.config_entry.options
        remind_after = options.get(CONF_REMINDER_MINUTES, DEFAULT_REMINDER_MINUTES)
        renew_before = options.get(CONF_RENEW_BEFORE_MINUTES, DEFAULT_RENEW_BEFORE_MINUTES)
        now = dt_util.now(TZ)
        live = {str(s.get("ID")) for s in active.values()}
        for sid in list(self._session_marks):
            if sid not in live:
                del self._session_marks[sid]
        changed = False
        for car, session in active.items():
            marks = self._session_marks.setdefault(str(session.get("ID")), {})
            started = local_time(session.get("DateIn"))
            ends = local_time(session.get("DateOut"))
            if (
                remind_after
                and started
                and not marks.get("reminded")
                and now - started >= timedelta(minutes=remind_after)
            ):
                marks["reminded"] = True
                changed = True
                minutes = int((now - started).total_seconds() // 60)
                self._fire("reminder", car, session, minutes=minutes)
                persistent_notification.async_create(
                    self.hass,
                    f"Car {car} has been parked with Cello for {minutes} minutes"
                    f" ({session.get('ZoneName') or 'zone unknown'}).",
                    title="Cello parking still running",
                    notification_id=f"{DOMAIN}_reminder_{car}",
                )
            if not ends or ends - now > timedelta(minutes=renew_before):
                continue
            if self.prefs(car).get("auto_renew"):
                if marks.get("renewed_for") == session.get("DateOut"):
                    continue
                marks["renewed_for"] = session.get("DateOut")
                changed = True
                try:
                    await self.client.extend_parking(
                        car, int(session["CityID"]), int(session["ZoneID"])
                    )
                except CelloConfirmationNeeded as err:
                    self._fire("renew_failed", car, session, message=str(err))
                    persistent_notification.async_create(
                        self.hass,
                        f"Cello asks to confirm before extending car {car}: {err}",
                        title="Cello auto-renew needs you",
                        notification_id=f"{DOMAIN}_renew_{car}",
                    )
                except CelloError as err:
                    self._fire("renew_failed", car, session, message=str(err))
                    _LOGGER.warning("Auto-renew of car %s failed: %s", car, err)
                else:
                    self._fire("renewed", car, session)
            elif not marks.get("ending_warned"):
                marks["ending_warned"] = True
                changed = True
                self._fire("ending_soon", car, session, ends=ends.isoformat())
        if changed:
            self.save()

    # --- actions ----------------------------------------------------------

    def resolve_zone(
        self, car: str, city: Any = None, zone: Any = None
    ) -> tuple[int, int]:
        """City/zone from the call, else the car's chosen zone."""
        if zone is not None:
            zone_id = int(zone)
            if city is not None:
                return int(city), zone_id
            for c in self.cities:
                if any(int(z["ID"]) == zone_id for z in c.get("Zones") or []):
                    return int(c["ID"]), zone_id
            raise HomeAssistantError(f"Zone {zone_id} is not a Cello zone")
        chosen = self.prefs(car).get("zone")
        if not chosen:
            raise HomeAssistantError(
                f"Pick a zone for car {car} first (its Zone selector or the zone field)"
            )
        return int(chosen[0]), int(chosen[1])

    async def async_start(
        self,
        car: str,
        *,
        city: Any = None,
        zone: Any = None,
        hours: float | None = None,
        until: datetime | None = None,
        accept_message: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if zone is None and self.locator is not None:
            # The car's own zone: make sure it is for where the car is now.
            await self.locator.ensure_zone_here(car, user_id)
        city_id, zone_id = self.resolve_zone(car, city, zone)
        if until is None:
            hours = self.prefs(car).get("hours", 0) if hours is None else hours
            if hours:
                until = dt_util.now(TZ) + timedelta(hours=float(hours))
        elif until.tzinfo:
            until = until.astimezone(TZ)
        geo = None
        if self.locator is not None:
            pos, _source = self.locator.car_position(car)
            if pos:
                geo = f"{pos[0]},{pos[1]}:{pos[2]}"
        pending = self._pending_confirm.pop(car, None)
        code = None
        if pending and dt_util.utcnow() - pending[2] < CONFIRM_WINDOW:
            code = pending[0]  # pressing Start again accepts the message
        try:
            body = await self.client.start_parking(
                car,
                city_id,
                zone_id,
                until=until.replace(tzinfo=None) if until else None,
                geo=geo,
                confirmation_code=code,
            )
        except CelloConfirmationNeeded as err:
            if accept_message:
                body = await self.client.start_parking(
                    car,
                    city_id,
                    zone_id,
                    until=until.replace(tzinfo=None) if until else None,
                    geo=geo,
                    confirmation_code=err.code,
                )
            else:
                self._pending_confirm[car] = (err.code, str(err), dt_util.utcnow())
                persistent_notification.async_create(
                    self.hass,
                    f"{err}\n\nPress Start parking again within 3 minutes to accept.",
                    title=f"Cello: confirm parking for car {car}",
                    notification_id=f"{DOMAIN}_confirm_{car}",
                )
                raise HomeAssistantError(
                    f"Cello asks to confirm: {err} — press Start again to accept"
                ) from err
        except CelloError as err:
            raise HomeAssistantError(f"Cello did not start parking: {err}") from err
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_confirm_{car}")
        await self.async_request_refresh()
        return body

    async def async_stop(self, car: str) -> dict[str, Any]:
        session = (self.data or {}).get("active", {}).get(car)
        if not session:
            await self.async_refresh()
            session = (self.data or {}).get("active", {}).get(car)
        if not session:
            raise HomeAssistantError(f"Car {car} has no running Cello parking")
        try:
            body = await self.client.stop_parking(int(session["ID"]))
        except CelloError as err:
            raise HomeAssistantError(f"Cello did not stop parking: {err}") from err
        await self.async_request_refresh()
        return body

    async def async_extend(self, car: str, accept_message: bool = False) -> dict[str, Any]:
        session = (self.data or {}).get("active", {}).get(car)
        if not session:
            raise HomeAssistantError(f"Car {car} has no running Cello parking")
        city_id, zone_id = int(session["CityID"]), int(session["ZoneID"])
        try:
            body = await self.client.extend_parking(car, city_id, zone_id)
        except CelloConfirmationNeeded as err:
            if not accept_message:
                raise HomeAssistantError(f"Cello asks to confirm: {err}") from err
            body = await self.client.extend_parking(
                car, city_id, zone_id, confirmation_code=err.code
            )
        except CelloError as err:
            raise HomeAssistantError(f"Cello did not extend parking: {err}") from err
        await self.async_request_refresh()
        return body

    async def async_set_service(self, name: str, enabled: bool) -> None:
        try:
            await self.client.set_service(name, enabled)
        except CelloError as err:
            raise HomeAssistantError(f"Cello did not change {name}: {err}") from err
        await self.async_request_refresh()
