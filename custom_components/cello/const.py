"""Constants for the Cello Parking integration."""
from __future__ import annotations

DOMAIN = "cello"
ATTRIBUTION = "Data from Cello (Cellopark)"

CONF_PHONE = "phone"
CONF_TOKEN = "token"
CONF_APP_KEY = "app_key"
CONF_BASE_URL = "base_url"
CONF_ACCOUNT_KEY = "account_key"
CONF_CUSTOMER_ID = "customer_id"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 120  # seconds
MIN_SCAN_INTERVAL = 30
TIME_ZONE = "Asia/Jerusalem"

CONF_REMINDER_MINUTES = "reminder_minutes"
DEFAULT_REMINDER_MINUTES = 120  # 0 = off
CONF_RENEW_BEFORE_MINUTES = "renew_before_minutes"
DEFAULT_RENEW_BEFORE_MINUTES = 5

EVENT_PARKING = "cello_parking"

# Per-car options: options["cars"][plate] = {...}
CONF_CARS = "cars"
CONF_TRACKER = "tracker"  # device_tracker / person on the car; empty = automatic (phones)
CONF_TRACKER_BACKUP = "tracker_backup"  # used while the main tracker has no position
CONF_DRIVE_ENTITY = "drive_entity"  # ignition / dashcam state / activity: on|driving = driving
CONF_NOTIFY = "notify"  # notify actions; empty = the phone that parked
CONF_MOVE_METERS = "move_meters"
DEFAULT_MOVE_METERS = 200

ATTR_CAR = "car"
ATTR_CITY = "city"
ATTR_ZONE = "zone"
ATTR_HOURS = "hours"
ATTR_UNTIL = "until"
ATTR_ACCEPT = "accept_message"
ATTR_LATITUDE = "latitude"
ATTR_LONGITUDE = "longitude"
