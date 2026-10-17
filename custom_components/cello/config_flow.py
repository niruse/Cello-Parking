"""Config flow for Cello Parking: phone number, then the texted code.

Nothing secret is typed into Home Assistant except the one-time SMS code; the
entry keeps the account token Cello returns, which stays valid until Cello
signs the device out (then Home Assistant asks for a new code).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import CelloApiClient, CelloAuthError, CelloError
from .const import (
    CONF_CARS,
    CONF_DRIVE_ENTITY,
    CONF_MOVE_METERS,
    CONF_NOTIFY,
    CONF_TRACKER,
    CONF_TRACKER_BACKUP,
    DEFAULT_MOVE_METERS,
    CONF_ACCOUNT_KEY,
    CONF_APP_KEY,
    CONF_BASE_URL,
    CONF_CUSTOMER_ID,
    CONF_PHONE,
    CONF_REMINDER_MINUTES,
    CONF_RENEW_BEFORE_MINUTES,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_REMINDER_MINUTES,
    DEFAULT_RENEW_BEFORE_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"
CONF_ACCOUNT = "account"
ACCOUNT_TYPES = {1: "Private", 2: "Business"}


def _clean_phone(phone: str) -> str:
    """Local Israeli format as the app sends it: 05XXXXXXXX."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits


class CelloConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Cello with an SMS code."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: CelloApiClient | None = None
        self._phone: str | None = None
        self._tokens: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = _clean_phone(user_input[CONF_PHONE])
            if len(phone) < 9:
                errors[CONF_PHONE] = "invalid_phone"
            else:
                self._phone = phone
                self._client = CelloApiClient(async_get_clientsession(self.hass))
                try:
                    await self._client.send_code(phone)
                except CelloError as err:
                    _LOGGER.warning("Cello did not send a code: %s", err)
                    errors["base"] = "cannot_send_code"
                else:
                    return await self.async_step_code()
        default = self._phone or ""
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PHONE, default=default): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEL)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client and self._phone
            try:
                self._tokens = await self._client.login(
                    self._phone, user_input[CONF_CODE].strip()
                )
            except CelloAuthError:
                errors["base"] = "invalid_code"
            except CelloError as err:
                _LOGGER.warning("Cello sign-in failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if len(self._tokens) > 1:
                    return await self.async_step_account()
                return await self._async_finish(self._tokens[0])
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            description_placeholders={"phone": self._phone or ""},
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the private or a business account when the phone has several."""
        if user_input is not None:
            chosen = next(
                t for t in self._tokens if str(t.get("ID")) == user_input[CONF_ACCOUNT]
            )
            return await self._async_finish(chosen)
        options = [
            {
                "value": str(t.get("ID")),
                "label": f"{ACCOUNT_TYPES.get(t.get('Type'), 'Account')} "
                f"({t.get('CustomerID')})",
            }
            for t in self._tokens
        ]
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT): SelectSelector(
                        SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def _async_finish(self, token: dict[str, Any]) -> ConfigFlowResult:
        assert self._client and self._phone
        customer = str(token.get("CustomerID") or token.get("ID"))
        data = {
            CONF_PHONE: self._phone,
            CONF_TOKEN: token["AuthorizationToken"],
            CONF_CUSTOMER_ID: customer,
            CONF_APP_KEY: self._client.app_key,
            CONF_BASE_URL: self._client.base_url,
            CONF_ACCOUNT_KEY: self._client.account_key,
        }
        await self.async_set_unique_id(customer)
        if self.source == "reauth":
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )
        self._abort_if_unique_id_configured()
        kind = ACCOUNT_TYPES.get(token.get("Type"), "")
        return self.async_create_entry(
            title="Cello" + (f" ({kind})" if kind else ""), data=data
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._phone = entry_data.get(CONF_PHONE)
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CelloOptionsFlow()


class CelloOptionsFlow(OptionsFlow):
    """General timing, and per car: its location device, drive signal and alerts."""

    _car: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["general", "pick_car"])

    async def async_step_pick_car(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        cars = list((self.config_entry.runtime_data.data or {}).get("cars", {}))
        if user_input is not None:
            self._car = user_input["car"]
            return await self.async_step_car()
        return self.async_show_form(
            step_id="pick_car",
            data_schema=vol.Schema(
                {vol.Required("car"): SelectSelector(SelectSelectorConfig(options=cars))}
            ),
        )

    async def async_step_car(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        car = self._car or ""
        all_cars = dict(self.config_entry.options.get(CONF_CARS) or {})
        if user_input is not None and CONF_MOVE_METERS in user_input:
            all_cars[car] = {
                CONF_TRACKER: user_input.get(CONF_TRACKER) or None,
                CONF_TRACKER_BACKUP: user_input.get(CONF_TRACKER_BACKUP) or None,
                CONF_DRIVE_ENTITY: user_input.get(CONF_DRIVE_ENTITY) or None,
                CONF_NOTIFY: user_input.get(CONF_NOTIFY) or [],
                CONF_MOVE_METERS: user_input[CONF_MOVE_METERS],
            }
            return self.async_create_entry(
                data={**self.config_entry.options, CONF_CARS: all_cars}
            )
        cur = all_cars.get(car, {})
        notify = sorted(
            f"{name}" for name in self.hass.services.async_services().get("notify", {})
            if name.startswith("mobile_app_")
        )
        return self.async_show_form(
            step_id="car",
            description_placeholders={"car": car},
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TRACKER, description={"suggested_value": cur.get(CONF_TRACKER)}
                    ): EntitySelector(EntitySelectorConfig(domain=["device_tracker", "person"])),
                    vol.Optional(
                        CONF_TRACKER_BACKUP,
                        description={"suggested_value": cur.get(CONF_TRACKER_BACKUP)},
                    ): EntitySelector(EntitySelectorConfig(domain=["device_tracker", "person"])),
                    vol.Optional(
                        CONF_DRIVE_ENTITY,
                        description={"suggested_value": cur.get(CONF_DRIVE_ENTITY)},
                    ): EntitySelector(EntitySelectorConfig(domain=["binary_sensor", "sensor"])),
                    vol.Optional(
                        CONF_NOTIFY, description={"suggested_value": cur.get(CONF_NOTIFY) or []}
                    ): SelectSelector(
                        SelectSelectorConfig(options=notify, multiple=True, custom_value=True)
                    ),
                    vol.Required(
                        CONF_MOVE_METERS,
                        default=cur.get(CONF_MOVE_METERS, DEFAULT_MOVE_METERS),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=50, max=5000, step=10, unit_of_measurement="m",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        opts = self.config_entry.options

        def minutes(lo: int, hi: int, step: int, unit: str = "min") -> NumberSelector:
            return NumberSelector(
                NumberSelectorConfig(
                    min=lo, max=hi, step=step, unit_of_measurement=unit,
                    mode=NumberSelectorMode.BOX,
                )
            )

        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): minutes(MIN_SCAN_INTERVAL, 3600, 10, "s"),
                    vol.Required(
                        CONF_REMINDER_MINUTES,
                        default=opts.get(CONF_REMINDER_MINUTES, DEFAULT_REMINDER_MINUTES),
                    ): minutes(0, 1440, 5),
                    vol.Required(
                        CONF_RENEW_BEFORE_MINUTES,
                        default=opts.get(
                            CONF_RENEW_BEFORE_MINUTES, DEFAULT_RENEW_BEFORE_MINUTES
                        ),
                    ): minutes(2, 60, 1),
                }
            ),
        )
