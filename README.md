# Home Assistant Flight Data

Live aircraft overhead in Home Assistant — position from **OpenSky Network**,
enriched with airline, aircraft type and route from **AeroDataBox**, shown on a
dashboard, pushed to your phone, and displayed on an **AWTRIX / Ulanzi** LED
matrix when something is close and low.

No custom component. Three small Python scripts and some YAML.

```
✈️ Atlantic Airways overhead
FLI416 · Airbus A320 · FAE → LGW · 4.5k up
```

---

## Why not the built-in `opensky` integration?

It authenticates with HTTP basic auth, which OpenSky has retired:

> *"OpenSky exclusively supports the OAuth2 client credentials flow. Basic
> authentication with username and password is no longer accepted."*

So it silently falls back to the **anonymous tier — 400 credits/day** instead of
4,000, and adding credentials changes nothing. You can verify this yourself: an
authenticated request and an anonymous one draw from the same IP-based bucket.

This project does the OAuth2 exchange properly and gets the full allowance.

---

## What you get

| Entity | What it is |
|---|---|
| `sensor.opensky_flights` | Count of aircraft in a bounding box, with a full `aircraft` attribute list |
| `sensor.awtrix_plane` | The nearest aircraft inside your distance/altitude thresholds, else `off` |
| `sensor.plane_details` | Airline, aircraft type, registration and route for that aircraft |
| `sensor.plane_log` | Recent sightings, for a history table |

Per aircraft you get: `icao24`, `callsign`, `airline`, `flight_number`,
`country`, `altitude_m`, `velocity_ms`, `heading`, `on_ground`, `distance_km`.

Everything is configurable from the dashboard at runtime — position, radius,
altitude ceiling and alert thresholds are `input_number` helpers, and the
`command_line` command is a template, so changes apply on the next poll with no
restart.

---

## Requirements

- Home Assistant with access to `configuration.yaml` (the `command_line`
  integration is used, so this will not work on a locked-down install)
- A free **OpenSky Network** account with an API client (OAuth2)
- Optional: a free **AeroDataBox** key via RapidAPI for the enrichment
- Optional: one or more **AWTRIX 3** displays and an MQTT broker

Python 3 only — no third-party libraries.

---

## Setup

**1. Copy the scripts**

```bash
cp bin/*.py /config/bin/
chmod +x /config/bin/*.py
```

**2. Add your credentials**

See `config/secrets.yaml.example`. OpenSky needs a client id and secret from
your account's API client page — *not* your login username and password.

**3. Merge the YAML**

- `config/configuration.yaml` — the `command_line` sensors and the helpers
- `config/template.yaml` — `sensor.awtrix_plane`
- `config/automations.yaml` — polling, enrichment and display automations

Replace `notify.mobile_app_your_phone` with your own notify service, and
`awtrix_display1` / `awtrix_display2` with your AWTRIX MQTT topics (or delete
the AWTRIX parts entirely — the rest works without them).

**4. Restart Home Assistant**

`command_line` sensors are only created at startup; there is no reload service.

**5. Set your location**

On the dashboard, or in Developer Tools, set `input_number.opensky_latitude`,
`opensky_longitude` and `opensky_radius` (metres).

**6. Optional: the dashboard**

`lovelace/flights-view.yaml` is the tab shown below — status, a live aircraft
table, a sightings history, and the settings.

---

## API limits — read this before raising the poll rate

**OpenSky** — 4,000 credits/day authenticated (400 anonymous).

Credit cost depends on the **area** of the bounding box, not the radius:

| Bounding box | Credits per call |
|---|---|
| ≤ 25 sq° | 1 |
| 25–100 sq° | 2 |
| 100–400 sq° | 3 |
| > 400 sq° or global | 4 |

A 20 km radius is about 0.2 sq°, so a single credit. You would need roughly a
275 km radius before it cost two.

Polling every 30 s over an 18.5 hour window is ~2,220 calls/day, about 56% of
the allowance.

**AeroDataBox (free RapidAPI plan)** — the binding limit is **not** the request
count:

| Quota | Limit |
|---|---|
| Requests | 2,400/month |
| **API units** | **600/month** ← the real constraint |

A lookup costs roughly 2.7 units, so about **220 lookups a month**. This project
therefore only enriches an aircraft that already meets your alert thresholds,
caches by callsign, and never calls the API on the regular poll.

---

## Gotchas worth knowing

**Cloudflare blocks `Python-urllib`.** RapidAPI returns `403` with
`error code: 1010` unless you send a `User-Agent` header. `curl` works, urllib
does not — which makes this baffling to debug.

**AeroDataBox returns HTTP 204, not 404,** when a callsign has no scheduled
flight. The body is empty, so naive JSON parsing throws and you lose the
response headers.

**Cache failures briefly, successes for longer.** A lookup can fail simply
because the flight is not in the schedule *yet* — one callsign here returned
nothing at 16:47 and full details minutes later. Negative results expire after
20 minutes; successful ones after 6 hours.

**YAML folded scalars (`>-`) preserve more-indented lines literally.** A
template split across two lines with extra indentation injects a newline into
the middle of your command, and Home Assistant fails with exit code 127. Keep
each template on one line.

**`command_line` needs a restart**, both to create a sensor and to pick up
changes to `json_attributes`.

**Poll rate cannot be measured from entity history** — `last_updated` only moves
when the data changes, so quiet periods look like missed polls.

---

## How it fits together

```
          ┌──────────────────┐
          │ OpenSky /states  │  OAuth2, token cached 30 min
          └────────┬─────────┘
                   │ every 30 s during service hours
          ┌────────▼─────────┐
          │ opensky_flights  │  count + aircraft list
          └────────┬─────────┘
                   │ nearest within thresholds
          ┌────────▼─────────┐
          │  awtrix_plane    │  'off' when nothing qualifies
          └────────┬─────────┘
                   │ ONLY when the callsign changes
          ┌────────▼─────────┐
          │  AeroDataBox     │  airline, type, route
          │  (cached)        │  falls back to the airframe
          └────────┬─────────┘        record if no flight
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
  dashboard     phone       AWTRIX app
                push        appears/disappears
```

The enrichment is deliberately event-driven. A flyover costs **one** API call,
not one per poll.

---

## Licence

MIT. See `LICENSE`.

Aircraft data from the [OpenSky Network](https://opensky-network.org/) and
[AeroDataBox](https://aerodatabox.com/). Check their terms before any commercial
use.
