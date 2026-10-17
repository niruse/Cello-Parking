"""Where each car is: zone suggestions from its location, and "it moved while parked".

A car's location comes from the device attached to it in the options (a
dashcam / car tracker, or a person) or, in automatic mode, from the phone that
was nearest the spot where the parking was started. Cello only maps GPS to a
city (its zones carry no coordinates), so a suggestion is "this city's zones",
best first: the zone this car parks in most there, else the city-wide zone.

While a parking runs, the car counts as driven away when its location moves
past the configured distance from the spot Cello stored at start, or when its
drive signal says driving (a phone: iOS/Android activity "Automotive"). That is
announced once per parking: event, notification, and a phone push with a
"Stop parking" button.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import slugify

from .api import CelloError
from .const import (
    CONF_CARS,
    CONF_DRIVE_ENTITY,
    CONF_MOVE_METERS,
    CONF_NOTIFY,
    CONF_TRACKER,
    CONF_TRACKER_BACKUP,
    DEFAULT_MOVE_METERS,
    DOMAIN,
)

if TYPE_CHECKING:
    from .coordinator import CelloCoordinator

_LOGGER = logging.getLogger(__name__)

DRIVE_STATES = {"on", "driving", "automotive", "in_vehicle", "moving"}
CITY_WIDE = "רחבי העיר"
NEAREST_PHONE_MAX_M = 500  # a phone further than this from the spot did not park the car
PHONE_FALLBACK_M = 2000  # a phone without an activity sensor: only a long move counts
DRIVE_SIGNAL_MIN_M = 50  # a drive signal with the car still at its spot is the parking manoeuvre
LOOKUP_EVERY_S = 300
LOOKUP_MOVE_M = 1000
MAX_ACCURACY_M = 100
SEARCH_LIMIT = 60
ACTION_STOP = "CELLO_STOP_"


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(
        (lon2 - lon1) / 2
    ) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def parse_geo(value: str | None) -> tuple[float, float, float] | None:
    """Cello's ``lat,lng`` or ``lat,lng:accuracy``."""
    if not value:
        return None
    try:
        point, _, acc = value.partition(":")
        lat, lng = (float(x) for x in point.split(","))
        return lat, lng, float(acc) if acc else 0.0
    except ValueError:
        return None


def state_position(state: State | None) -> tuple[float, float, float] | None:
    if state is None:
        return None
    lat, lng = state.attributes.get(ATTR_LATITUDE), state.attributes.get(ATTR_LONGITUDE)
    if lat is None or lng is None:
        return None
    return float(lat), float(lng), float(state.attributes.get("gps_accuracy") or 0)


# A point Cello does not recognise takes the nearest city it named within this.
NEARBY_CITY_M = 5000
MAX_CITY_HITS = 300


class CarLocator:
    """Location, suggestions and moved-while-parked for every car of one account."""

    def __init__(self, hass: HomeAssistant, coordinator: CelloCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        # plate -> {"city": {...}, "position": (lat, lng, acc), "source": entity, ...}
        self.where: dict[str, dict[str, Any]] = {}
        self.suggestions: dict[str, list[str]] = {}
        self.search: dict[str, str] = {}
        self._lookups: dict[str, tuple[float, tuple[float, float]]] = {}
        self._unsubs: list = []

    # --- options ----------------------------------------------------------

    def car_options(self, car: str) -> dict[str, Any]:
        return (self.coordinator.config_entry.options.get(CONF_CARS) or {}).get(car, {})

    def trackers(self, car: str) -> list[str]:
        """The car's attached devices: the main one, then the backup."""
        opts = self.car_options(car)
        return [t for t in (opts.get(CONF_TRACKER), opts.get(CONF_TRACKER_BACKUP)) if t]

    def tracker(self, car: str) -> str | None:
        """The attached device to trust now: the main one while it has a
        position, else the backup (for example a dashcam that lost GPS and a
        head unit that still has it)."""
        trackers = self.trackers(car)
        for tracker in trackers:
            if state_position(self.hass.states.get(tracker)):
                return tracker
        return trackers[0] if trackers else None

    def move_meters(self, car: str) -> float:
        return float(self.car_options(car).get(CONF_MOVE_METERS) or DEFAULT_MOVE_METERS)

    # --- phones -------------------------------------------------------------

    def phones(self) -> list[str]:
        """GPS device trackers of the Home Assistant companion app."""
        reg = er.async_get(self.hass)
        return [
            e.entity_id
            for e in reg.entities.values()
            if e.domain == "device_tracker" and e.platform == "mobile_app" and not e.disabled
        ]

    def _device_entity(self, entity_id: str, domain: str, suffix: str) -> str | None:
        reg = er.async_get(self.hass)
        entry = reg.async_get(entity_id)
        if not entry or not entry.device_id:
            return None
        for e in er.async_entries_for_device(reg, entry.device_id):
            if e.domain == domain and e.entity_id.endswith(suffix):
                return e.entity_id
        return None

    def activity_sensor(self, phone: str) -> str | None:
        return self._device_entity(phone, "sensor", "_activity")

    def notify_service(self, entity_id: str) -> str | None:
        """The companion app's notify action for the phone behind ``entity_id``."""
        entry = er.async_get(self.hass).async_get(entity_id)
        if not entry or not entry.device_id:
            return None
        device = dr.async_get(self.hass).async_get(entry.device_id)
        if not device:
            return None
        service = f"mobile_app_{slugify(device.name_by_user or device.name or '')}"
        return service if self.hass.services.has_service("notify", service) else None

    def nearest_phone(self, point: tuple[float, float]) -> tuple[str, float] | None:
        best = None
        for phone in self.phones():
            pos = state_position(self.hass.states.get(phone))
            if pos:
                d = distance_m(point, pos[:2])
                if best is None or d < best[1]:
                    best = (phone, d)
        return best

    def phone_of_user(self, user_id: str | None) -> str | None:
        """The pressing user's phone: their person's GPS tracker."""
        if not user_id:
            return None
        for state in self.hass.states.async_all("person"):
            if state.attributes.get("user_id") == user_id:
                for tracker in state.attributes.get("device_trackers") or []:
                    if tracker in self.phones() and state_position(self.hass.states.get(tracker)):
                        return tracker
        return None

    # --- start / stop -------------------------------------------------------

    @callback
    def async_start(self) -> None:
        watched: set[str] = set(self.phones())
        for phone in list(watched):
            if activity := self.activity_sensor(phone):
                watched.add(activity)
        for car in self.coordinator.data.get("cars", {}):
            opts = self.car_options(car)
            watched.update(self.trackers(car))
            if opts.get(CONF_DRIVE_ENTITY):
                watched.add(opts[CONF_DRIVE_ENTITY])
        if watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, sorted(watched), self._on_state)
            )
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._on_action)
        )
        self._unsubs.append(self.coordinator.async_add_listener(self._on_coordinator))
        self._on_coordinator()
        for car in self.coordinator.data.get("cars", {}):
            if self.tracker(car):
                self.hass.async_create_task(self._auto_suggest(car))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _on_coordinator(self) -> None:
        for car in self.coordinator.data.get("cars", {}):
            self._evaluate(car)

    @callback
    def _on_state(self, event: Event) -> None:
        for car in self.coordinator.data.get("cars", {}):
            self._evaluate(car)
            if event.data["entity_id"] in self.trackers(car):
                self.hass.async_create_task(self._auto_suggest(car))

    # --- where is the car ---------------------------------------------------

    def car_position(self, car: str) -> tuple[tuple[float, float, float] | None, str | None]:
        """(position, source entity) from the attached device, else the parking's phone."""
        if tracker := self.tracker(car):
            return state_position(self.hass.states.get(tracker)), tracker
        marks = self.coordinator.session_marks(car)
        phone = (marks or {}).get("phone")
        if phone:
            return state_position(self.hass.states.get(phone)), phone
        return None, None

    async def _city_at(self, lat: float, lng: float) -> dict[str, Any] | None:
        try:
            near = await self.coordinator.client.get_locations_near(lat, lng)
        except CelloError as err:
            _LOGGER.debug("No Cello city at %s,%s: %s", lat, lng, err)
            return None
        return near[0] if near else None

    async def lookup_city(self, lat: float, lng: float) -> dict[str, Any] | None:
        """The Cello city at a point, with its zones (Cello names the city only).

        Cello's own lookup only answers at a few exact spots of a city (in
        one city, 160 m from a working spot already gets "no city"). So every
        spot where it did name a city is remembered, and a point it does not
        recognise takes the nearest remembered city within NEARBY_CITY_M;
        ``nearby_m`` then says how far that spot was.
        """
        hits = self.coordinator.city_hits
        city = await self._city_at(lat, lng)
        distance = 0
        if city is not None:
            # Remember the spot, unless one already sits within 100 m.
            if not any(
                h[2] == city.get("ID") and distance_m((lat, lng), (h[0], h[1])) < 100
                for h in hits
            ):
                hits.append([lat, lng, city.get("ID")])
                del hits[:-MAX_CITY_HITS]
                self.coordinator.save()
                # A car that found no city before may be near this one.
                for other, where in list(self.where.items()):
                    if not where.get("city"):
                        self._lookups.pop(other, None)
                        self.hass.async_create_task(self._auto_suggest(other))
        else:
            near = [(distance_m((lat, lng), (h[0], h[1])), h[2]) for h in hits]
            near = [n for n in near if n[0] <= NEARBY_CITY_M]
            if not near:
                return None
            distance, city_id = min(near)
            city = next(
                (c for c in self.coordinator.cities if c.get("ID") == city_id), None
            )
            if city is None:
                return None
        known = {c.get("ID"): c for c in self.coordinator.cities}
        if not city.get("Zones"):
            city = {**city, "Zones": (known.get(city.get("ID")) or {}).get("Zones") or []}
        if distance:
            city = {**city, "nearby_m": round(distance)}
        return city

    def ranked_zones(self, car: str, city: dict[str, Any]) -> list[str]:
        """The city's zone labels, best first: this car's habit, then city-wide."""
        from .coordinator import zone_label

        habits = dict(self.coordinator.prefs(car).get("zone_counts") or {})
        zones = list(city.get("Zones") or [])
        # This month's parkings name the place as "city, zone": count those too.
        city_name = (city.get("Name") or "").strip()
        by_place = {f"{city_name}, {(z.get('Name') or '').strip()}": str(z["ID"]) for z in zones}
        for t in (self.coordinator.data or {}).get("month") or []:
            if str(t.get("CarNumber")) == car and (zid := by_place.get((t.get("Location") or "").strip())):
                habits[zid] = int(habits.get(zid, 0)) + 1
        zones.sort(
            key=lambda z: (
                -int(habits.get(str(z["ID"]), 0)),
                0 if (z.get("Name") or "").strip() == CITY_WIDE or z.get("Code") == "01" else 1,
                z.get("Name") or "",
            )
        )
        return [zone_label(city, z) for z in zones]

    async def suggest(
        self, car: str, lat: float, lng: float, source: str | None, *, apply: bool
    ) -> list[str]:
        city = await self.lookup_city(lat, lng)
        self.where[car] = {
            **self.where.get(car, {}),
            "city": city,
            "position": (lat, lng),
            "source": source,
        }
        ranked = self.ranked_zones(car, city) if city else []
        self.suggestions[car] = ranked
        if apply and ranked:
            ids = self.coordinator.zone_options.get(ranked[0])
            if ids:
                self.coordinator.set_pref(car, "zone", list(ids))
        self.coordinator.async_update_listeners()
        return ranked

    async def zone_from_location(self, car: str, user_id: str | None = None) -> list[str]:
        """Pick the zone where the car (or the pressing user's phone) is now."""
        pos, source = None, None
        if tracker := self.tracker(car):
            pos, source = state_position(self.hass.states.get(tracker)), tracker
        if pos is None and (phone := self.phone_of_user(user_id)):
            pos, source = state_position(self.hass.states.get(phone)), phone
        if pos is None:
            # Nobody known: the phone that reported most recently.
            fresh = [
                s for p in self.phones() if (s := self.hass.states.get(p)) and state_position(s)
            ]
            if fresh:
                latest = max(fresh, key=lambda s: s.last_updated)
                pos, source = state_position(latest), latest.entity_id
        if pos is None:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(
                f"No location for car {car}: attach a tracker in the Cello options"
            )
        ranked = await self.suggest(car, pos[0], pos[1], source, apply=True)
        if not ranked:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError("Cello has no parking zones at this location")
        return ranked

    async def _auto_suggest(self, car: str) -> None:
        """An attached tracker moved: re-suggest when it reached a new place."""
        if car in self.coordinator.data.get("active", {}):
            return
        pos = state_position(self.hass.states.get(self.tracker(car) or ""))
        if not pos:
            return
        last = self._lookups.get(car)
        now = time.monotonic()
        if last and (
            now - last[0] < LOOKUP_EVERY_S or distance_m(last[1], pos[:2]) < LOOKUP_MOVE_M
        ):
            return
        self._lookups[car] = (now, pos[:2])
        await self.suggest(car, pos[0], pos[1], self.tracker(car), apply=True)

    # --- search -------------------------------------------------------------

    def set_search(self, car: str, text: str) -> None:
        self.search[car] = text.strip()
        self.coordinator.async_update_listeners()

    def options_for(self, car: str) -> list[str]:
        """The suggestions select: search results, else zones where the car is."""
        query = self.search.get(car, "").casefold()
        if query:
            words = query.split()
            return [
                label
                for label in self.coordinator.zone_options
                if all(w in label.casefold() for w in words)
            ][:SEARCH_LIMIT]
        return self.suggestions.get(car, [])

    # --- moved while parked -------------------------------------------------

    @callback
    def _evaluate(self, car: str) -> None:
        session = self.coordinator.data.get("active", {}).get(car)
        marks = self.coordinator.session_marks(car)
        if not session or marks is None:
            return
        # Where Cello says the parking started; a parking started without GPS
        # (not from the app) takes the attached tracker's position at first sight.
        spot = parse_geo(session.get("GeoLocation")) or (
            tuple(marks["spot"]) if marks.get("spot") else None
        )
        tracker = self.tracker(car)
        if spot is None and tracker:
            if pos := state_position(self.hass.states.get(tracker)):
                spot = pos
                marks["spot"] = list(pos)
                self.coordinator.save()
        if spot is None:
            return
        if "phone" not in marks:
            # The phone that parked: the car's stand-in without a tracker, and who to tell.
            near = self.nearest_phone(spot[:2])
            marks["phone"] = near[0] if near and near[1] <= NEAREST_PHONE_MAX_M else None
            self.coordinator.save()
        pos, source = self.car_position(car)
        dist = None
        if pos:
            slack = min(pos[2], MAX_ACCURACY_M) + min(spot[2], MAX_ACCURACY_M)
            dist = max(0.0, distance_m(spot[:2], pos[:2]) - slack)
        self.where[car] = {
            **self.where.get(car, {}),
            "source": source,
            "distance_m": round(dist) if dist is not None else None,
            "spot": spot[:2],
        }
        if marks.get("moved"):
            return
        reason = self._moved_reason(car, marks, dist)
        if reason:
            marks["moved"] = reason
            self.coordinator.save()
            self.hass.async_create_task(self._announce_moved(car, session, reason, dist))
            self.coordinator.async_update_listeners()

    def _moved_reason(self, car: str, marks: dict[str, Any], dist: float | None) -> str | None:
        limit = self.move_meters(car)
        if self.tracker(car):
            drive = self.car_options(car).get(CONF_DRIVE_ENTITY)
            state = self.hass.states.get(drive) if drive else None
            driving = state is not None and state.state.casefold() in DRIVE_STATES
            if driving and (dist is None or dist > DRIVE_SIGNAL_MIN_M):
                return "driving"
            if dist is not None and dist > limit:
                return "moved"
            return None
        phone = marks.get("phone")
        if not phone or dist is None:
            return None
        activity = self.activity_sensor(phone)
        state = self.hass.states.get(activity) if activity else None
        if state is not None and state.state not in ("unavailable", "unknown"):
            in_car = state.state.casefold() in DRIVE_STATES
            return "driving" if in_car and dist > limit else None
        return "moved" if dist > max(limit, PHONE_FALLBACK_M) else None

    def notify_targets(self, car: str) -> list[str]:
        targets = list(self.car_options(car).get(CONF_NOTIFY) or [])
        if not targets:
            marks = self.coordinator.session_marks(car) or {}
            for entity in (marks.get("phone"), self.tracker(car)):
                if entity and (service := self.notify_service(entity)):
                    targets.append(service)
                    break
        return targets

    async def _announce_moved(
        self, car: str, session: dict[str, Any], reason: str, dist: float | None
    ) -> None:
        zone = session.get("ZoneName") or "?"
        far = f" ({round(dist)} m away)" if dist else ""
        message = (
            f"Car {car} seems to be {'driving' if reason == 'driving' else 'moved'}{far}"
            f" while Cello parking in {zone} is still running."
        )
        self.coordinator.fire("moved", car, session, reason=reason, distance_m=dist)
        persistent_notification.async_create(
            self.hass, message, title="Cello: parking still running",
            notification_id=f"{DOMAIN}_moved_{car}",
        )
        for target in self.notify_targets(car):
            try:
                await self.hass.services.async_call(
                    "notify",
                    target,
                    {
                        "title": "🅿️ Parking still running",
                        "message": message,
                        "data": {
                            "tag": f"cello_moved_{car}",
                            "push": {"interruption-level": "time-sensitive"},
                            "actions": [
                                {"action": f"{ACTION_STOP}{car}", "title": "Stop parking",
                                 "destructive": True},
                                {"action": "CELLO_KEEP", "title": "Keep it running"},
                            ],
                        },
                    },
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Cello could not notify %s: %s", target, err)

    async def _on_action(self, event: Event) -> None:
        action = str(event.data.get("action") or "")
        if not action.startswith(ACTION_STOP):
            return
        car = action[len(ACTION_STOP):]
        if car not in self.coordinator.data.get("cars", {}):
            return
        try:
            await self.coordinator.async_stop(car)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Stop from the notification failed for %s: %s", car, err)
            persistent_notification.async_create(
                self.hass, f"Could not stop parking for car {car}: {err}",
                title="Cello", notification_id=f"{DOMAIN}_moved_{car}",
            )
            return
        persistent_notification.async_create(
            self.hass, f"Parking for car {car} was stopped from the notification.",
            title="Cello", notification_id=f"{DOMAIN}_moved_{car}",
        )
