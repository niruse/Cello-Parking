"""Where each car is: zone suggestions from its location, and "it moved while parked".

A car's location comes from the device attached to it in the options (a
dashcam / car tracker, or a person) or, in automatic mode, from the phone that
was nearest the spot where the parking was started. Cello only maps GPS to a
city (its zones carry no coordinates), so a suggestion is "this city's zones",
best first: the zone this car parks in most there, else the city-wide zone.

The car's Parking zone follows its location only once it has parked: every
drive signal of its devices off (a dashcam's ignition or state, a phone's
activity) and its position still for a few minutes. Passing a city on the way
no longer picks that city, and a spot where Cello has no zone clears the zone
instead of keeping the last city passed. A zone picked by hand stays until
the car drives again, and Start parking re-checks a zone that was set far
from where the car is now.

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
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import slugify

from .api import CelloError, CelloNotFound
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
# Parked = drive signals off and position still this long. Longer than a red
# light, and than the one-to-two-minute gaps a dashcam's cloud status shows mid-drive.
STOP_SETTLE_S = 180
STILL_M = 100  # a move shorter than this keeps the car "still"
REAPPLY_M = 150  # a parked spot this far from where the zone was set gets a fresh look
START_STALE_M = 1000  # Start parking re-checks a zone set further than this from the car
LOOKUP_RETRY_S = 300  # Cello could not be asked: try again after this


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
# Setting or clearing a parked car's zone trusts that far only spots where a
# parked car's lookup named the city; any other remembered spot counts within
# PASSING_CITY_M, so a city the car merely drove past is not picked for a spot
# where Cello has no zone.
PASSING_CITY_M = 2000
MAX_CITY_HITS = 300
# zone_manual: the zone was picked by hand while parked (kept until the car
# drives off) or on the way (kept for the stop ahead).
MANUAL_PARKED = "parked"
MANUAL_DRIVING = "driving"


class StopWatch:
    """When one car counts as parked: drive signals off and position still."""

    def __init__(self) -> None:
        self.still_at: tuple[float, float] | None = None
        self.still_since = 0.0
        self.quiet_since: float | None = None  # drive signals off since
        self.drive_since: float | None = None  # drive signals on since

    def update(
        self, now: float, driving: bool | None, point: tuple[float, float] | None
    ) -> float | None:
        """Seconds until the car counts as parked (0: parked), None while it drives.

        ``driving`` is None for a car without any drive signal: then only the
        position being still counts.
        """
        if point is not None and (
            self.still_at is None or distance_m(self.still_at, point) > STILL_M
        ):
            self.still_at, self.still_since = point, now
        if driving:
            self.quiet_since = None
            if self.drive_since is None:
                self.drive_since = now
            return None
        self.drive_since = None
        if self.quiet_since is None:
            self.quiet_since = now
        return max(0.0, max(self.quiet_since, self.still_since) + STOP_SETTLE_S - now)

    def live_while_driving(self) -> bool:
        """Whether the position moved during this drive (a dashcam that only
        reports at ignition on/off still shows where the drive began)."""
        return self.drive_since is not None and self.still_since > self.drive_since


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
        self._watches: dict[str, StopWatch] = {}
        self._timers: dict[str, Any] = {}
        self._applying: set[str] = set()
        self._retry_at: dict[str, float] = {}
        self._stopped = False

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

    def drive_entities(self, car: str) -> list[str]:
        """Everything that tells whether the car drives: the drive signal set in
        the options, plus the ignition (or a phone's activity) of every device
        attached to the car.

        One device alone can be wrong: a head unit that stays awake through a
        quick engine restart never reports the second ignition, and a
        dashcam's cloud status drops to offline for a minute mid-drive.
        """
        found: list[str] = []
        if drive := self.car_options(car).get(CONF_DRIVE_ENTITY):
            found.append(drive)
        for tracker in self.trackers(car):
            for extra in (
                self._device_entity(tracker, "binary_sensor", "_ignition"),
                self.activity_sensor(tracker),
            ):
                if extra and extra not in found:
                    found.append(extra)
        return found

    def driving(self, car: str) -> bool | None:
        """True while any drive signal says driving, False when every one that
        reports says not, None when the car has no signal that reports."""
        known = [
            state
            for entity in self.drive_entities(car)
            if (state := self.hass.states.get(entity)) is not None
            and state.state not in ("unavailable", "unknown")
        ]
        if not known:
            return None
        return any(state.state.casefold() in DRIVE_STATES for state in known)

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
            watched.update(self.trackers(car))
            watched.update(self.drive_entities(car))
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
            self._check_parked(car)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    @callback
    def _on_coordinator(self) -> None:
        for car in self.coordinator.data.get("cars", {}):
            self._evaluate(car)
            if car not in self.coordinator.data.get("active", {}) and car not in self._timers:
                self._check_parked(car)  # e.g. a parking just ended

    @callback
    def _on_state(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        for car in self.coordinator.data.get("cars", {}):
            self._evaluate(car)
            trackers = self.trackers(car)
            if entity_id in trackers:
                self.hass.async_create_task(self._auto_suggest(car))
            if entity_id in trackers or entity_id in self.drive_entities(car):
                self._check_parked(car)

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
        """Cello's city at a point; None when Cello names none there.

        Raises CelloError when Cello could not be asked.
        """
        try:
            near = await self.coordinator.client.get_locations_near(lat, lng)
        except CelloNotFound:
            return None
        return near[0] if near else None

    async def lookup_city(
        self,
        lat: float,
        lng: float,
        *,
        strict: bool = False,
        remember: bool = True,
        habit: frozenset = frozenset(),
    ) -> dict[str, Any] | None:
        """The Cello city at a point, with its zones (Cello names the city only).

        Cello's own lookup only answers at a few exact spots of a city (in
        one city, 160 m from a working spot already gets "no city"). So every
        spot where it did name a city is remembered, and a point it does not
        recognise takes the nearest remembered city within NEARBY_CITY_M;
        ``nearby_m`` then says how far that spot was.

        ``strict`` is for a parked car whose zone is set (or cleared) from the
        answer: a spot remembered from such a lookup counts within
        NEARBY_CITY_M, any other within PASSING_CITY_M, and a lookup Cello did
        not answer (with no remembered spot near) raises CelloError instead of
        meaning "no city", so an outage does not clear the zone. A lookup made
        while driving passes ``remember=False``: the road past a city is not
        that city. A city in ``habit`` (where the car parks) counts within
        NEARBY_CITY_M too: a spot near a city the car keeps parking in is
        that city, unlike one near a city it only drove past.
        """
        hits = self.coordinator.city_hits
        try:
            city = await self._city_at(lat, lng)
            failed: CelloError | None = None
        except CelloError as err:
            _LOGGER.debug("Cello city lookup at %s,%s failed: %s", lat, lng, err)
            city, failed = None, err
        distance = 0
        if city is not None:
            if remember and not self._stopped:
                self._remember_city(lat, lng, city.get("ID"), parked=strict)
        else:
            near = []
            for h in hits:
                d = distance_m((lat, lng), (h[0], h[1]))
                trusted = (len(h) > 3 and bool(h[3])) or h[2] in habit
                if d <= (NEARBY_CITY_M if trusted or not strict else PASSING_CITY_M):
                    near.append((d, h[2]))
            if not near:
                if strict and failed is not None:
                    raise failed
                return None
            distance, city_id = min(near)
            city = next(
                (c for c in self.coordinator.cities if c.get("ID") == city_id), None
            )
            if city is None:
                return None
        # The GPS answer only names the city: its Zones are empty, or (seen
        # 2026-10-19) one placeholder "city-wide" with ID 0 that is not a real zone
        # and matched nothing, so no zone was offered. The full list is the truth.
        name = (city.get("Name") or "").strip()
        full = next(
            (c for c in self.coordinator.cities if c.get("ID") == city.get("ID")), None
        ) or next(
            (c for c in self.coordinator.cities if (c.get("Name") or "").strip() == name),
            None,
        )
        if full and full.get("Zones"):
            city = {**city, "ID": full.get("ID"), "Zones": full["Zones"]}
        else:
            city = {**city, "Zones": [z for z in city.get("Zones") or [] if z.get("ID")]}
        if distance:
            city = {**city, "nearby_m": round(distance)}
        return city

    def _remember_city(self, lat: float, lng: float, city_id: Any, *, parked: bool) -> None:
        """Remember a spot where Cello named the city (``parked``: for a parked
        car), unless one already sits within 100 m."""
        hits = self.coordinator.city_hits
        for h in hits:
            if h[2] == city_id and distance_m((lat, lng), (h[0], h[1])) < 100:
                if parked and not (len(h) > 3 and h[3]):
                    h[3:] = [1]
                    self.coordinator.save()
                return
        hits.append([lat, lng, city_id, 1] if parked else [lat, lng, city_id])
        del hits[:-MAX_CITY_HITS]
        self.coordinator.save()
        # A car that found no city before may be near this one.
        for other, where in list(self.where.items()):
            if not where.get("city"):
                self._lookups.pop(other, None)
                self.hass.async_create_task(self._auto_suggest(other))

    def habit_cities(self, car: str) -> frozenset:
        """Cities this car has paid to park in with Cello."""
        zones = {str(z) for z in (self.coordinator.prefs(car).get("zone_counts") or {})}
        return frozenset(
            city for city, zone in self.coordinator.zone_options.values() if str(zone) in zones
        )

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
        self,
        car: str,
        lat: float,
        lng: float,
        source: str | None,
        *,
        apply: bool,
        strict: bool = False,
        remember: bool = True,
    ) -> list[str]:
        """Look up the zones at a point (best first); ``apply`` makes the best
        one the car's Parking zone, as if picked by hand."""
        city = await self.lookup_city(
            lat, lng, strict=strict, remember=remember, habit=self.habit_cities(car)
        )
        self.where[car] = {
            **self.where.get(car, {}),
            "city": city,
            "position": (lat, lng),
            "source": source,
        }
        ranked = self.ranked_zones(car, city) if city else []
        self.suggestions[car] = ranked
        if apply and ranked:
            self.choose_zone(car, ranked[0])
        self.coordinator.async_update_listeners()
        return ranked

    # --- the car's Parking zone ---------------------------------------------

    def set_zone(
        self,
        car: str,
        label: str | None,
        point: tuple[float, float] | None,
        *,
        manual: str | None = None,
    ) -> None:
        """Make ``label`` the car's Parking zone (None: no zone) for the spot
        ``point``. ``manual`` (MANUAL_PARKED / MANUAL_DRIVING) marks a zone
        picked by hand; otherwise the next parked spot replaces it."""
        ids = self.coordinator.zone_options.get(label) if label else None
        prefs = self.coordinator.prefs(car)
        prefs["zone"] = list(ids) if ids else None
        prefs["zone_at"] = [point[0], point[1]] if point else None
        prefs["zone_manual"] = manual
        self.coordinator.save()
        self.coordinator.async_update_listeners()

    def choose_zone(self, car: str, label: str | None) -> None:
        """The user picked the zone: kept until the car drives off from here,
        or, picked on the way, for the stop ahead. None (the "—" option) means
        no zone, back to automatic."""
        pos, _source = self.car_position(car)
        point = (pos[0], pos[1]) if pos else None
        if label is None:
            self.set_zone(car, None, point)
        elif self.driving(car):
            self.set_zone(car, label, None, manual=MANUAL_DRIVING)
        else:
            self.set_zone(car, label, point, manual=MANUAL_PARKED)

    def _schedule(self, car: str, delay: float) -> None:
        if cancel := self._timers.pop(car, None):
            cancel()
        self._timers[car] = async_call_later(
            self.hass, delay, partial(self._check_parked, car)
        )

    @callback
    def _check_parked(self, car: str, _now: datetime | None = None) -> None:
        """Set the zone once the car has parked; never while it drives."""
        if self._stopped:
            return
        if cancel := self._timers.pop(car, None):
            cancel()
        prefs = self.coordinator.prefs(car)
        pos, _source = self.car_position(car)
        point = (pos[0], pos[1]) if pos else None
        driving = self.driving(car)
        at = prefs.get("zone_at")
        manual = prefs.get("zone_manual")
        if manual and manual != MANUAL_DRIVING and (
            driving
            or (
                driving is None
                and point
                and at
                and distance_m((at[0], at[1]), point) > REAPPLY_M
            )
        ):
            # Driving off from where it was picked ends a hand-picked zone
            # (kept in the store, so a restart in between does not lose that).
            prefs["zone_manual"] = None
            self.coordinator.save()
        wait = self._watches.setdefault(car, StopWatch()).update(
            time.monotonic(), driving, point
        )
        stale = self._note_drive(car, driving, point)
        if wait == 0 and point and prefs.get("zone_manual") == MANUAL_DRIVING:
            # Picked on the way: it is this stop's zone now.
            prefs["zone_manual"] = MANUAL_PARKED
            prefs["zone_at"] = [point[0], point[1]]
            self.coordinator.save()
        if car in self.coordinator.data.get("active", {}):
            return  # a parking runs: its zone is Cello's
        if wait is None or point is None:
            return
        if wait > 0:
            self._schedule(car, wait + 1)
            return
        if prefs.get("zone_manual"):
            return
        if stale:
            # It drove, yet its device still shows where the drive began (it
            # was offline the whole way): where it is now is unknown, so no
            # zone rather than the zone of where it left from.
            if prefs.get("zone") or prefs.get("zone_at"):
                _LOGGER.debug("Car %s drove without a new position: zone cleared", car)
                self.set_zone(car, None, None)
            return
        at = prefs.get("zone_at")
        if at and distance_m((at[0], at[1]), point) <= REAPPLY_M:
            return  # already set for this spot
        if (retry := self._retry_at.get(car, 0.0) - time.monotonic()) > 0:
            self._schedule(car, retry + 1)  # Cello failed a moment ago
            return
        if car not in self._applying:
            self.hass.async_create_task(self._apply_parked(car, point))

    def _note_drive(
        self, car: str, driving: bool | None, point: tuple[float, float] | None
    ) -> bool:
        """Remember where each drive began; True while the car's position is
        still exactly that spot after the drive (its device sent nothing on
        the way, e.g. a dashcam offline for the whole trip)."""
        prefs = self.coordinator.prefs(car)
        drive = prefs.get("drive")
        here = [round(point[0], 6), round(point[1], 6)] if point else None
        if driving:
            if drive is None:
                prefs["drive"] = {"from": here}
                self.coordinator.save()
            return False
        if drive is None:
            return False
        if here is None or drive.get("from") is None or here != drive["from"]:
            prefs["drive"] = None  # a new position arrived: it is fresh again
            self.coordinator.save()
            return False
        return True

    def position_unknown(self, car: str) -> bool:
        """The car drove but its device has not said where it is since."""
        drive = self.coordinator.prefs(car).get("drive")
        if not drive or drive.get("from") is None or self.driving(car):
            return False
        pos, _source = self.car_position(car)
        return pos is not None and [round(pos[0], 6), round(pos[1], 6)] == drive["from"]

    async def _apply_parked(self, car: str, point: tuple[float, float]) -> None:
        """The car parked at ``point``: its zone is the best one there, or none."""
        self._applying.add(car)
        prefs = self.coordinator.prefs(car)
        before = (prefs.get("zone"), prefs.get("zone_at"), prefs.get("zone_manual"))
        try:
            ranked = await self.suggest(
                car, point[0], point[1], self.tracker(car), apply=False, strict=True
            )
        except CelloError as err:
            _LOGGER.debug("Zone for parked car %s not looked up: %s", car, err)
            if not self._stopped:
                self._retry_at[car] = time.monotonic() + LOOKUP_RETRY_S
                self._schedule(car, LOOKUP_RETRY_S)
            return
        finally:
            self._applying.discard(car)
        self._retry_at.pop(car, None)
        if (
            self._stopped
            or car in self.coordinator.data.get("active", {})
            or self.driving(car)
            or (prefs.get("zone"), prefs.get("zone_at"), prefs.get("zone_manual")) != before
        ):
            return  # unloaded, parking started, drove off, or picked meanwhile
        _LOGGER.debug("Car %s parked: zone %s", car, ranked[0] if ranked else None)
        self.set_zone(car, ranked[0] if ranked else None, point)

    async def ensure_zone_here(self, car: str, user_id: str | None = None) -> None:
        """Before Start parking without a zone given: the car's zone must be
        for where the car is now. Otherwise it is looked up again; if Cello has
        no zone here, or cannot be asked, Start is refused."""
        prefs = self.coordinator.prefs(car)
        if prefs.get("zone") and prefs.get("zone_manual"):
            return  # picked by hand for this stop
        pos, source = self.car_position(car)
        watch = self._watches.get(car)
        if self.driving(car) and not (watch and watch.live_while_driving()):
            # The engine still runs and the tracker only reports at ignition
            # on/off: it shows where the drive began. Use the user's phone.
            phone = self.phone_of_user(user_id)
            pos = state_position(self.hass.states.get(phone)) if phone else None
            source = phone
            if pos is None:
                raise HomeAssistantError(
                    f"Car {car} is still driving: turn the engine off, or pick the zone"
                )
        elif self.position_unknown(car):
            # It drove without its device reporting: the position is where
            # the drive began. Use the user's phone, or ask for the zone.
            phone = self.phone_of_user(user_id)
            pos = state_position(self.hass.states.get(phone)) if phone else None
            source = phone
            if pos is None:
                raise HomeAssistantError(
                    f"Car {car}'s device has not reported where it is since it drove:"
                    " pick the zone"
                )
        if pos is None:
            return  # nothing to check against
        here = (pos[0], pos[1])
        at = prefs.get("zone_at")
        set_m = distance_m((at[0], at[1]), here) if prefs.get("zone") and at else None
        if set_m is not None and set_m <= REAPPLY_M:
            return
        try:
            ranked = await self.suggest(car, here[0], here[1], source, apply=False, strict=True)
        except CelloError as err:
            if set_m is not None and set_m <= START_STALE_M:
                return  # Cello is out, and the zone was set close by
            raise HomeAssistantError(
                f"Cello could not be asked where car {car} is ({err}): pick the zone first"
            ) from err
        if not ranked:
            raise HomeAssistantError(
                f"Cello has no parking zone where car {car} is: pick the zone first"
            )
        self.set_zone(car, ranked[0], here)

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
            raise HomeAssistantError(
                f"No location for car {car}: attach a tracker in the Cello options"
            )
        ranked = await self.suggest(car, pos[0], pos[1], source, apply=True)
        if not ranked:
            raise HomeAssistantError("Cello has no parking zones at this location")
        return ranked

    async def _auto_suggest(self, car: str) -> None:
        """An attached tracker moved: refresh the suggestions when it reached a
        new place. Only the suggestions: the zone itself waits until it parks."""
        if self._stopped or car in self.coordinator.data.get("active", {}):
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
        await self.suggest(
            car, pos[0], pos[1], self.tracker(car), apply=False,
            remember=self.driving(car) is not True,
        )

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
            driving = bool(self.driving(car))
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
