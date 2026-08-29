#!/usr/bin/env python3
"""Enrich one aircraft callsign with airline, type and route from AeroDataBox.

OpenSky gives position but no route, aircraft type or real airline name -- only
a callsign, from which we can guess the operator by its 3-letter prefix.
AeroDataBox resolves the rest, but it is a metered commercial API, so this is
built to be called as little as possible:

  * ONLY for an aircraft that already qualifies for the Awtrix plane app, never
    for every aircraft in the search box.
  * Results are cached by callsign, so a flyover costs ONE request rather than
    one per 30-second OpenSky poll.
  * Negative results are cached too, otherwise an unknown callsign would be
    re-requested on every poll for as long as it is overhead.

Measured limits on the free RapidAPI BASIC plan (2026-08-28):
  2,400 requests/month, and a strict PER-SECOND limit -- two back-to-back calls
  returned HTTP 429. Hence the retry-with-backoff below.

It also appends each sighting to a JSONL log, which sensor.plane_log reads back
for the "Recent flights overhead" card. Logging happens here because this script
already runs exactly once per new overhead aircraft.

Usage:
  aerodatabox_lookup.py CALLSIGN [ALTITUDE_M] [DISTANCE_KM]

Always prints one JSON object. On any failure it prints valid JSON with
"ok": false so the sensor degrades rather than going unavailable.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "aerodatabox.p.rapidapi.com"
SECRETS = "/config/secrets.yaml"
CACHE = "/config/.aerodatabox_cache.json"
CACHE_TTL = 6 * 3600          # a flight's route does not change mid-flight
# Failures are cached far more briefly than successes. A lookup can fail simply
# because the flight is not in the schedule YET -- LUA681K returned nothing at
# 16:47 and full details minutes later. A 6-hour negative cache would have kept
# showing nothing all evening.
NEG_TTL = 20 * 60
CACHE_MAX = 200               # keep the file small; evict oldest
LOG = "/config/.plane_log.jsonl"
LOG_MAX = 200                 # keep the last N sightings
LOG_DEDUPE = 30 * 60          # do not re-log the same callsign within 30 min
TIMEOUT = 25
RETRY_WAIT = 3                # seconds, for the per-second 429


def read_key():
    with open(SECRETS) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("aerodatabox_key:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("aerodatabox_key missing from secrets.yaml")


META_KEY = "_quota"          # reserved key in the cache file; not a callsign


def load_cache():
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(c):
    keys = [k for k in c if k != META_KEY]
    if len(keys) > CACHE_MAX:
        for k in sorted(keys, key=lambda k: c[k].get("ts", 0))[:len(keys) - CACHE_MAX]:
            c.pop(k, None)
    try:
        with open(CACHE, "w") as fh:
            json.dump(c, fh)
    except OSError:
        pass


def fetch(callsign, key):
    url = f"https://{HOST}/flights/callsign/{urllib.parse.quote(callsign)}"
    # A User-Agent is REQUIRED: RapidAPI sits behind Cloudflare, which rejects
    # urllib's default "Python-urllib/3.x" with 403 error 1010 (banned browser
    # signature). curl works because it sends its own UA. Caught 2026-08-28.
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": key,
        "X-RapidAPI-Host": HOST,
        "User-Agent": "HomeAssistant-AeroDataBox/1.0",
        "Accept": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                # RapidAPI reports the monthly quota in headers. Captured on
                # every real call and persisted, because a cache hit makes no
                # request and so has no fresh headers to read.
                # RapidAPI reports THREE quotas. "api-units" is the binding
                # one on the free plan: 600/month against 2,400 requests, and a
                # call costs ~2.7 units -- so ~220 calls/month, not 2,400.
                meta = {
                    "req_remaining": r.headers.get("x-ratelimit-requests-remaining"),
                    "req_limit": r.headers.get("x-ratelimit-requests-limit"),
                    "unit_remaining": r.headers.get("x-ratelimit-api-units-remaining"),
                    "unit_limit": r.headers.get("x-ratelimit-api-units-limit"),
                    "ts": int(time.time()),
                }
                body = r.read().decode().strip()
                # 204 = no flight matches this callsign. A normal outcome, not
                # an error -- and the quota headers are still valid, so they must
                # be returned rather than lost to a JSON parse failure.
                if r.status == 204 or not body:
                    return None, "no flight found", meta
                return json.loads(body), None, meta
        except urllib.error.HTTPError as e:
            # 429 here is the per-second cap, not the monthly quota -- worth one retry
            if e.code == 429 and attempt == 1:
                time.sleep(RETRY_WAIT)
                continue
            if e.code == 404:
                return None, "not found", None
            return None, f"HTTP {e.code}", None
        except Exception as e:                      # noqa: BLE001
            return None, f"{type(e).__name__}", None
    return None, "rate limited", None


def fetch_aircraft(icao24, key):
    """Fall back to the airframe record when there is no flight record.

    /flights/callsign only covers SCHEDULED flights, so private, positioning and
    military traffic returns 204 -- no airline, no type, nothing. /aircrafts is
    TIER 1 and works for any airframe, giving registration, type and operator.
    No route, because an airframe record has no route, but far better than blank.

    Only called when the flight lookup found nothing, to conserve API units.
    """
    url = f"https://{HOST}/aircrafts/icao24/{urllib.parse.quote(icao24)}"
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": key,
        "X-RapidAPI-Host": HOST,
        "User-Agent": "HomeAssistant-AeroDataBox/1.0",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode().strip()
            if r.status == 204 or not body:
                return None
            d = json.loads(body)
            if not isinstance(d, dict):
                return None
            return {
                "airline": d.get("airlineName"),
                "aircraft_model": d.get("typeName") or d.get("model"),
                "registration": d.get("reg"),
                "flight_number": None,
                "origin": None, "origin_name": None,
                "destination": None, "destination_name": None,
                "status": "airframe only",
            }
    except Exception:                               # noqa: BLE001
        return None


def summarise(payload):
    """Flatten the API response to the handful of fields worth displaying."""
    if not payload:
        return None
    fl = payload[0] if isinstance(payload, list) else payload
    if not isinstance(fl, dict):
        return None
    dep = fl.get("departure") or {}
    arr = fl.get("arrival") or {}
    dep_ap = dep.get("airport") or {}
    arr_ap = arr.get("airport") or {}
    ac = fl.get("aircraft") or {}
    al = fl.get("airline") or {}

    def code(ap):
        # iata is nicer to read; fall back to icao, then a shortened name
        return ap.get("iata") or ap.get("icao") or (ap.get("name") or "")[:12] or None

    return {
        "airline": al.get("name"),
        "flight_number": fl.get("number"),
        "aircraft_model": ac.get("model"),
        "registration": ac.get("reg"),
        "origin": code(dep_ap),
        "origin_name": dep_ap.get("name"),
        "destination": code(arr_ap),
        "destination_name": arr_ap.get("name"),
        "status": fl.get("status"),
    }


def quota_fields(cache):
    """Last known RapidAPI quota, so the sensor shows it even on a cache hit."""
    m = cache.get(META_KEY) or {}
    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {
        "adb_requests_remaining": as_int(m.get("req_remaining")),
        "adb_requests_limit": as_int(m.get("req_limit")),
        "adb_units_remaining": as_int(m.get("unit_remaining")),
        "adb_units_limit": as_int(m.get("unit_limit")),
        "adb_checked": m.get("ts"),
    }


def append_log(entry):
    """Append one sighting, deduped by callsign, trimmed to the last LOG_MAX."""
    try:
        rows = []
        try:
            with open(LOG) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except (OSError, ValueError):
            rows = []

        # An HA restart can re-trigger the lookup for an aircraft still
        # overhead; without this the same flight would appear twice.
        for r in reversed(rows[-10:]):
            if r.get("callsign") == entry["callsign"] and \
               (entry["ts"] - r.get("ts", 0)) < LOG_DEDUPE:
                return
        rows.append(entry)
        rows = rows[-LOG_MAX:]
        with open(LOG, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    except OSError:
        pass          # logging must never break the sensor


def main():
    # "-" is the placeholder the command_line sensor passes when no aircraft
    # qualifies. Quoting an empty argument made Home Assistant run the command
    # through a shell that could not find python3 (exit 127), so the command is
    # now unquoted and this sentinel stands in for the empty case.
    if len(sys.argv) < 2 or sys.argv[1].strip() in ("", "-", "none", "None"):
        # No aircraft overhead. Still report the last known quota, otherwise the
        # dashboard blanks out whenever the sky is quiet -- which is most of the
        # time, and exactly when you might want to check the budget.
        out = {"ok": False, "reason": "no callsign"}
        out.update(quota_fields(load_cache()))
        print(json.dumps(out))
        return
    callsign = sys.argv[1].strip().upper()

    def num(i):
        try:
            return float(sys.argv[i])
        except (IndexError, ValueError):
            return None
    altitude_m, distance_km = num(2), num(3)
    icao24 = sys.argv[4].strip().lower() if len(sys.argv) > 4 else ""
    if icao24 in ("-", "none"):
        icao24 = ""

    cache = load_cache()
    hit = cache.get(callsign)
    ttl = CACHE_TTL if (hit or {}).get("data") else NEG_TTL
    if hit and (time.time() - hit.get("ts", 0)) < ttl:
        out = dict(hit.get("data") or {})
        out.update({"ok": bool(hit.get("data")), "callsign": callsign, "cached": True})
        out.update(quota_fields(cache))
        append_log({"ts": int(time.time()), "callsign": callsign,
                    "airline": out.get("airline"), "model": out.get("aircraft_model"),
                    "origin": out.get("origin"), "destination": out.get("destination"),
                    "altitude_m": altitude_m, "distance_km": distance_km})
        print(json.dumps(out))
        return

    try:
        key = read_key()
    except Exception as e:                          # noqa: BLE001
        print(json.dumps({"ok": False, "callsign": callsign, "reason": str(e)}))
        return

    payload, err, meta = fetch(callsign, key)
    if meta and meta.get("unit_remaining"):
        cache[META_KEY] = meta
    data = summarise(payload) if not err else None

    # No scheduled flight for this callsign -- try the airframe instead.
    if data is None and icao24:
        time.sleep(1.5)                 # respect the per-second rate limit
        data = fetch_aircraft(icao24, key)
        if data:
            err = None

    # cache negatives too, so an unknown callsign is not re-requested every poll
    cache[callsign] = {"ts": time.time(), "data": data}
    save_cache(cache)

    out = dict(data or {})
    out.update({"ok": data is not None, "callsign": callsign, "cached": False})
    out.update(quota_fields(cache))
    if err:
        out["reason"] = err
    append_log({"ts": int(time.time()), "callsign": callsign,
                "airline": out.get("airline"), "model": out.get("aircraft_model"),
                "origin": out.get("origin"), "destination": out.get("destination"),
                "altitude_m": altitude_m, "distance_km": distance_km})
    print(json.dumps(out))


if __name__ == "__main__":
    main()
