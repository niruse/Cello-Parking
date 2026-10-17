"""Sensors: account money (balance, this month, invoices, card) and, per car,
the running parking plus the car's spend this month."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CelloConfigEntry
from .coordinator import local_time
from .entity import CelloAccountEntity, CelloCarEntity

LAST_PARKINGS = 10


def _parking(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "car": t.get("CarNumber"),
        "where": t.get("Location"),
        "started": t.get("DateIn"),
        "ended": t.get("DateOut"),
        "price": t.get("Price"),
    }


def _latest(items: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    dated = [(local_time(i.get(key)), i) for i in items]
    dated = [d for d in dated if d[0]]
    return max(dated, key=lambda d: d[0])[1] if dated else None


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]
    attrs: Callable[[dict[str, Any]], dict[str, Any]] = lambda d: {}


def _month_attrs(d: dict[str, Any]) -> dict[str, Any]:
    month = sorted(
        d.get("month") or [],
        key=lambda t: local_time(t.get("DateIn")) or 0,
        reverse=True,
    )
    return {
        "parkings": len(month),
        "report_pdf": d.get("month_pdf"),
        "last_parkings": [_parking(t) for t in month[:LAST_PARKINGS]],
    }


def _invoice(d: dict[str, Any]) -> dict[str, Any] | None:
    invoices = d.get("invoices") or []
    return _latest([{**i, "DateX": f"{i.get('Date')} 00:00:00"} for i in invoices], "DateX")


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="ILS",
        suggested_display_precision=2,
        value=lambda d: d["status"].get("AccountBalance"),
        attrs=lambda d: {
            "account_name": d["status"].get("AccountName"),
            "billing_program": d["status"].get("BillingProgramName"),
        },
    ),
    AccountSensorDescription(
        key="month_spend",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="ILS",
        suggested_display_precision=2,
        value=lambda d: round(sum(float(t.get("Price") or 0) for t in d.get("month") or []), 2),
        attrs=_month_attrs,
    ),
    AccountSensorDescription(
        key="month_parkings",
        state_class=SensorStateClass.TOTAL,
        value=lambda d: len(d.get("month") or []),
    ),
    AccountSensorDescription(
        key="last_invoice",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="ILS",
        suggested_display_precision=2,
        value=lambda d: (_invoice(d) or {}).get("Total"),
        attrs=lambda d: {
            "date": (_invoice(d) or {}).get("Date"),
            "paid": (_invoice(d) or {}).get("Paid"),
            "url": (_invoice(d) or {}).get("URL"),
            "invoices_this_year": len(d.get("invoices") or []),
        },
    ),
    AccountSensorDescription(
        key="payment_card",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: ((d.get("payment") or {}).get("Number") or "")[-4:] or None,
        attrs=lambda d: {"expires": (d.get("payment") or {}).get("ExpairedDate")},
    ),
)


@dataclass(frozen=True, kw_only=True)
class CarSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any] | None, list[dict[str, Any]]], Any]


def _session(fn: Callable[[dict[str, Any]], Any]):
    return lambda s, month: fn(s) if s else None


CAR_SENSORS: tuple[CarSensorDescription, ...] = (
    CarSensorDescription(key="zone", value=_session(lambda s: s.get("ZoneName"))),
    CarSensorDescription(
        key="started",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=_session(lambda s: local_time(s.get("DateIn"))),
    ),
    CarSensorDescription(
        key="ends",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=_session(lambda s: local_time(s.get("DateOut"))),
    ),
    CarSensorDescription(
        key="cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="ILS",
        suggested_display_precision=2,
        value=_session(lambda s: s.get("Amount")),
    ),
    CarSensorDescription(
        key="car_month_spend",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="ILS",
        suggested_display_precision=2,
        value=lambda s, month: round(sum(float(t.get("Price") or 0) for t in month), 2),
    ),
    CarSensorDescription(
        key="last_parking",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda s, month: local_time((_latest(month, "DateIn") or {}).get("DateIn")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: CelloConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        CelloAccountSensor(coordinator, d) for d in ACCOUNT_SENSORS
    ]
    for car in coordinator.data["cars"]:
        entities.extend(CelloCarSensor(coordinator, car, d) for d in CAR_SENSORS)
        entities.append(CelloCarLocationSensor(coordinator, car))
        entities.append(CelloTimeLeftSensor(coordinator, car))
    async_add_entities(entities)


class CelloAccountSensor(CelloAccountEntity, SensorEntity):
    entity_description: AccountSensorDescription

    def __init__(self, coordinator, description: AccountSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attrs(self.coordinator.data)


class CelloCarSensor(CelloCarEntity, SensorEntity):
    entity_description: CarSensorDescription

    def __init__(self, coordinator, car: str, description: CarSensorDescription) -> None:
        super().__init__(coordinator, car, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        month = [
            t
            for t in self.coordinator.data.get("month") or []
            if str(t.get("CarNumber")) == self.car
        ]
        return self.entity_description.value(self.session, month)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "last_parking":
            return None
        month = [
            t
            for t in self.coordinator.data.get("month") or []
            if str(t.get("CarNumber")) == self.car
        ]
        last = _latest(month, "DateIn")
        return _parking(last) if last else None


class CelloCarLocationSensor(CelloCarEntity, SensorEntity):
    """The Cello city where the car is, and how far it is from where it parked."""

    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "car_location")

    @property
    def _where(self) -> dict[str, Any]:
        return self.coordinator.locator.where.get(self.car) or {}

    @property
    def native_value(self) -> str | None:
        city = self._where.get("city")
        return city.get("Name") if city else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        locator = self.coordinator.locator
        where = self._where
        marks = self.coordinator.session_marks(self.car) or {}
        current, _source = locator.car_position(self.car)
        position = current or where.get("position") or (None, None)
        return {
            "source": where.get("source") or locator.tracker(self.car) or "automatic (phones)",
            "latitude": position[0],
            "longitude": position[1],
            "parked_phone": marks.get("phone"),
            "distance_from_parking_m": where.get("distance_m"),
            "moved": marks.get("moved"),
            "suggested_zones": locator.suggestions.get(self.car, [])[:10],
        }


class CelloTimeLeftSensor(CelloCarEntity, SensorEntity):
    """Minutes until the running parking's end time (0 when past it, None without one)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator, car: str) -> None:
        super().__init__(coordinator, car, "time_left")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, timedelta(seconds=30))
        )

    @callback
    def _tick(self, _now) -> None:
        if self.session:
            self.async_write_ha_state()

    def _span(self) -> tuple[Any, Any]:
        s = self.session or {}
        return local_time(s.get("DateIn")), local_time(s.get("DateOut"))

    @property
    def native_value(self) -> int | None:
        _start, end = self._span()
        if not end:
            return None
        return max(0, int((end - dt_util.now()).total_seconds() // 60))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        start, end = self._span()
        total = int((end - start).total_seconds() // 60) if start and end else None
        elapsed = int((dt_util.now() - start).total_seconds() // 60) if start else None
        return {"total_minutes": total, "elapsed_minutes": elapsed}
