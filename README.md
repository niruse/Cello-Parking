# Cello for Home Assistant

Unofficial Home Assistant integration for **Cello** (formerly Cellopark), the Israeli street-parking app.

Sign in with your phone number and the SMS code Cello texts you. Every car on the account becomes a device.

## Per car
- **Parking** (on while a parking runs), zone, start, Cello's end time, running cost
- **Start parking / Stop parking / Extend parking** buttons
- **Parking zone** picker and **Park for** (hours, 0 = until stopped) used by Start
- **Auto-renew**: Home Assistant extends the parking a few minutes before Cello would end it
- Spent this month, last parking

## Account
- Balance, spent this month (with the last parkings and the month's PDF report), parkings this month
- Last invoice, payment card (last 4 digits)
- Cello's own **auto-extend** and **auto-stop** services

## Actions
`cello.start_parking` (car, optional city/zone, hours or until, accept_message), `cello.stop_parking`,
`cello.extend_parking`, `cello.find_zones` (latitude/longitude → cities and zones).

## Events
`cello_parking` with `type`: `started`, `ended`, `reminder`, `ending_soon`, `renewed`, `renew_failed`.

## Options
Refresh interval, reminder after N minutes (0 = off), auto-renew lead time.

Starting, extending and stopping parking costs real money on your Cello account.
