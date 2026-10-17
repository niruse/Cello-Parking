"""Client for the Cello (formerly Cellopark) smartphone API.

The app talks plain JSON to one Israeli backend. Every call carries a fixed
set of headers; signed-in calls add ``Authorization: Basic <token>`` where the
token is used as-is. The real outcome is the ``Status`` field of the JSON
body, not the HTTP status.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://application.cellopark.co.il/smartphone-api/"
APP_VERSION = "12.31"
# Sent once as the body of App/RegisterApp; the server answers with an App-Key.
REGISTER_SECRET = (
    "o4/N0/nH2tL1Wak/D6/Pv4vcUW7KKt0oRy1f/o2RSAZtseOJTp0Ibd5m6fEjt86F-Ver1.0"
)
DEVICE_NAME = "HomeAssistant"
OS_VERSION = "34"
DATE_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M")

# StatusPark values of StartParking / ExtendParking / StopParking.
PARK_STARTED = 1
PARK_ENDED = 3
PARK_CLOSED_STILL_ACTIVE = 4
PARK_NEEDS_CONFIRMATION = 5
PARK_ALREADY_ACTIVE = 10
PARK_FREE_NOW = 11
PARK_NOT_FOUND = 15


class CelloError(Exception):
    """The API refused a request or could not be reached."""


class CelloAuthError(CelloError):
    """The sign-in token is no longer accepted."""


class CelloAppKeyError(CelloError):
    """The App-Key expired; the client has to register again."""


class CelloConfirmationNeeded(CelloError):
    """Starting needs the user to accept a message first (StatusPark 5/11)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def parse_date(value: str | None) -> datetime | None:
    """Parse the app's ``dd-MM-yyyy HH:mm:ss`` local time (no zone)."""
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def format_date(value: datetime) -> str:
    """Format a local datetime the way StartParking expects it."""
    return value.strftime(DATE_FORMATS[0])


class CelloApiClient:
    """One signed-in (or signing-in) Cello app instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str | None = None,
        app_key: str | None = None,
        token: str | None = None,
        account_key: str | None = None,
    ) -> None:
        self._session = session
        self.base_url = base_url or DEFAULT_BASE_URL
        self.app_key = app_key
        self.token = token
        self.account_key = account_key
        self._session_id = str(uuid.uuid4())

    def _headers(self, auth: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "App-Version": APP_VERSION,
            "Device-OS": "android",
            "Device-Name": DEVICE_NAME,
            "OS-Version": OS_VERSION,
            "Accept-Language": "he-IL",
            "SessionID": self._session_id,
            "User-Agent": (
                f"Device-{DEVICE_NAME},OS-android,App_version-{APP_VERSION}"
                f"-{self.account_key or 'null'}"
            ),
        }
        if self.app_key:
            headers["App-Key"] = self.app_key
        if auth and self.token:
            headers["Authorization"] = f"Basic {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        auth: bool = True,
        _retry: bool = True,
    ) -> dict[str, Any]:
        if not self.app_key and path != "App/RegisterApp":
            await self.register()
        url = self.base_url + path
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._headers(auth),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                _LOGGER.debug("%s %s -> %s %s", method, path, resp.status, text[:500])
                if resp.status == 401:
                    body: dict[str, Any] = {"Status": 401}
                else:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError as err:
                        raise CelloError(
                            f"{path}: HTTP {resp.status}, not JSON"
                        ) from err
        except aiohttp.ClientError as err:
            raise CelloError(f"{path}: {err}") from err
        if not isinstance(body, dict):
            return {"Status": 200, "Value": body}
        status = body.get("Status", 200)
        if status == 401:
            if body.get("Key") == "App-Key" and _retry:
                self.app_key = None
                await self.register()
                return await self._request(
                    method, path, params=params, json=json, auth=auth, _retry=False
                )
            raise CelloAuthError(body.get("Message") or "Signed out")
        if status not in (200, None):
            raise CelloError(body.get("Message") or f"{path}: Status {status}")
        return body

    # --- registration and sign-in -------------------------------------

    async def register(self) -> None:
        """Register this client as an app install; gives App-Key + domain."""
        body = await self._request(
            "POST", "App/RegisterApp", json=REGISTER_SECRET, auth=False
        )
        config = body.get("AppConfig") or {}
        if not config.get("AppKey"):
            raise CelloError("RegisterApp returned no AppKey")
        self.app_key = config["AppKey"]
        domain = config.get("Domain")
        if domain:
            self.base_url = domain if domain.endswith("/") else domain + "/"

    async def send_code(self, phone: str) -> None:
        """Ask Cello to text a sign-in code to the phone."""
        await self._request(
            "GET", "Account/VerifyPhone", params={"userName": phone}, auth=False
        )

    async def login(self, phone: str, code: str) -> list[dict[str, Any]]:
        """Sign in with the texted code; returns the account token list."""
        body = await self._request(
            "POST",
            "Account/Login",
            json={"UserName": phone, "Password": "", "SecretCode": code},
            auth=False,
        )
        tokens = body.get("UserTokenList") or []
        if not tokens:
            raise CelloAuthError(body.get("Message") or "No account for this code")
        return tokens

    # --- reads ------------------------------------------------------------

    async def get_status(self) -> dict[str, Any]:
        body = await self._request("GET", "App/GetStatus")
        if body.get("AccountKey"):
            self.account_key = str(body["AccountKey"])
        return body

    async def get_cars(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "Car")
        return body.get("Cars") or []

    async def get_active_sessions(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "ParkingSession/GetActiveParkingSessions")
        return body.get("ActiveParks") or []

    async def get_locations(self) -> list[dict[str, Any]]:
        body = await self._request(
            "GET", "Location/GetLocations", params={"lastUpdate": ""}
        )
        return body.get("Cities") or []

    async def get_locations_near(self, lat: float, lng: float) -> list[dict[str, Any]]:
        body = await self._request(
            "GET",
            "Location/GetLocationsByGeoLocation",
            params={"lat": str(lat), "lng": str(lng)},
        )
        return body.get("Cities") or []

    async def get_transactions(self, year: int, month: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            "Info/GetTransactionReport",
            params={"year": year, "month": month, "byBill": "false"},
        )

    async def get_payment_method(self) -> dict[str, Any]:
        body = await self._request("GET", "Payment/GetCurrentPaymentMethod")
        return body.get("PaymentMethodData") or {}

    async def calculate_max_time(
        self, car: str, city_id: int, zone_id: int
    ) -> dict[str, Any]:
        body = await self._request(
            "POST",
            "ParkingSession/CalculateMaxTime",
            json={"CarNumber": car, "CityID": city_id, "ZoneID": zone_id},
        )
        return body.get("Data") or {}

    # --- parking --------------------------------------------------------

    async def _park(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = await self._request("POST", path, json=payload)
        status = body.get("StatusPark")
        if status in (PARK_STARTED, PARK_ALREADY_ACTIVE):
            return body
        if status in (PARK_NEEDS_CONFIRMATION, PARK_FREE_NOW) and body.get(
            "ConfirmCode"
        ):
            raise CelloConfirmationNeeded(
                body.get("Message") or "Confirmation needed", body["ConfirmCode"]
            )
        raise CelloError(body.get("Message") or f"{path}: StatusPark {status}")

    async def start_parking(
        self,
        car: str,
        city_id: int,
        zone_id: int,
        *,
        until: datetime | None = None,
        geo: str | None = None,
        confirmation_code: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "CarNumber": car,
            "CityID": city_id,
            "ZoneID": zone_id,
            "Source": 2,
        }
        if until:
            payload["MaxDateTime"] = format_date(until)
        if geo:
            payload["GeoLocation"] = geo
        if confirmation_code:
            payload["ConfirmationCode"] = confirmation_code
        return await self._park("ParkingSession/StartParking", payload)

    async def extend_parking(
        self,
        car: str,
        city_id: int,
        zone_id: int,
        *,
        confirmation_code: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "CarNumber": car,
            "CityID": city_id,
            "ZoneID": zone_id,
            "Source": 2,
        }
        if confirmation_code:
            payload["ConfirmationCode"] = confirmation_code
        return await self._park("ParkingSession/ExtendParking", payload)

    async def stop_parking(self, transaction_id: int) -> dict[str, Any]:
        body = await self._request(
            "POST",
            "ParkingSession/StopParking",
            json={"TransactionID": transaction_id, "MethodClose": 1, "Source": 2},
        )
        status = body.get("StatusPark")
        if status not in (PARK_ENDED, PARK_CLOSED_STILL_ACTIVE):
            raise CelloError(body.get("Message") or f"Stop: StatusPark {status}")
        return body

    # --- account services and money -------------------------------------

    async def set_service(self, name: str, enabled: bool) -> None:
        """Switch one Cello account service, e.g. AutoExtensionPark."""
        await self._request(
            "POST", "AccountSettings/ActivateServices", json={name: enabled}
        )

    async def get_invoices(self, year: int) -> list[dict[str, Any]]:
        body = await self._request("GET", "Info/GetInvoices", params={"year": year})
        return body.get("List") or []
