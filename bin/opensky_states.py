#!/usr/bin/env python3
"""Query the OpenSky /states/all endpoint for a bounding box, with OAuth2.

Called by the "OpenSky Flights" command_line sensor. Replaces the built-in
opensky integration, which still uses basic auth -- OpenSky retired that, so
the integration silently falls back to the anonymous 400-credit/day tier.
With OAuth2 client credentials the account gets 4,000/day instead.

Home Assistant cannot do this natively:
  * the REST platform has no OAuth2 client-credentials flow, and
  * the token is a ~1,450-character JWT, so it cannot be cached in an
    input_text (255-char limit).

So the token is cached in a file and reused until shortly before it expires
(they last 30 minutes). Only a token refresh costs an extra HTTP request; it
does not consume API credits, which are spent by /states/all calls.

Credentials are read from /config/secrets.yaml (opensky_client_id and
opensky_client_secret) rather than passed as arguments. That frees the
command_line "command" to be a Home Assistant template, so the position,
radius and altitude ceiling can come from input_number helpers and be changed
from the UI without a restart.

Usage:
  opensky_states.py LAT LON RADIUS_M [MAX_ALTITUDE_M]
  MAX_ALTITUDE_M of 0 (or omitted) means no ceiling.

Prints one JSON object on stdout. On failure it still prints valid JSON with
count -1 and an "error" key, so the sensor shows a clear problem state rather
than going unavailable with no explanation.
"""
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
             "/protocol/openid-connect/token")
STATES_URL = "https://opensky-network.org/api/states/all"
CACHE = "/config/.opensky_token.json"
SECRETS = "/config/secrets.yaml"
TIMEOUT = 25

# OpenSky state vector indices (see their REST docs)
I_ICAO, I_CALLSIGN, I_COUNTRY = 0, 1, 2
I_LON, I_LAT = 5, 6
I_ON_GROUND = 8
I_VELOCITY, I_TRACK = 9, 10
I_GEO_ALT = 13


# ICAO airline designators -> operator name. The first three letters of a
# callsign identify the operator; the remainder is the flight number.
#
# Deliberately a small hand-picked list rather than the full ICAO register:
# these are the operators actually seen overhead here, plus the major UK and
# European carriers. Anything unrecognised falls back to the raw prefix, so an
# unknown airline shows as e.g. "XYZ" rather than being hidden.
#
# Note a callsign without a 3-letter alpha prefix (e.g. "GFRGP") is usually a
# registration, meaning private or general aviation, not an airline.
AIRLINES = {
    "BAW": "British Airways", "SHT": "British Airways Shuttle",
    "EZY": "easyJet", "EJU": "easyJet Europe", "EZS": "easyJet Switzerland",
    "RYR": "Ryanair", "RUK": "Ryanair UK",
    "TOM": "TUI Airways", "EXS": "Jet2", "LOG": "Loganair",
    "VIR": "Virgin Atlantic", "BCS": "European Air Transport",
    "AAL": "American", "UAL": "United", "DAL": "Delta",
    "ACA": "Air Canada", "AFR": "Air France", "DLH": "Lufthansa",
    "KLM": "KLM", "SWR": "Swiss", "AUA": "Austrian", "SAS": "SAS",
    "IBE": "Iberia", "VLG": "Vueling", "TAP": "TAP Air Portugal",
    "AEE": "Aegean", "THY": "Turkish", "FIN": "Finnair", "NAX": "Norwegian",
    "WZZ": "Wizz Air", "WUK": "Wizz Air UK", "ELY": "El Al",
    "UAE": "Emirates", "QTR": "Qatari", "ETD": "Etihad", "SIA": "Singapore",
    "QFA": "Qantas", "ANA": "All Nippon", "JAL": "Japan Airlines",
    "CPA": "Cathay Pacific", "AIC": "Air India",
    "FDX": "FedEx", "UPS": "UPS", "GEC": "Lufthansa Cargo",
    "NPT": "West Atlantic", "CLX": "Cargolux",
    "RRR": "RAF", "RCH": "US Air Mobility Command", "CFC": "Canadian Forces",
    "NJE": "NetJets", "EJA": "NetJets US", "LNX": "Luxaviation",
    "GAF": "German Air Force", "BEL": "Brussels Airlines",
    "EIN": "Aer Lingus", "EXP": "Exxaero", "MMD": "Air Alsie",
}


def resolve_airline(callsign):
    """Map a callsign to an operator name.

    Returns (airline, flight_number). Callsigns are OPERATOR + FLIGHT, e.g.
    "RYR47RK" -> Ryanair, 47RK. A callsign that is not 3 letters + digits is
    treated as a registration (private/GA) rather than an airline.
    """
    if not callsign:
        return None, None
    cs = callsign.strip().upper()
    prefix, rest = cs[:3], cs[3:]
    if len(cs) > 3 and prefix.isalpha() and any(c.isdigit() for c in rest):
        return AIRLINES.get(prefix, prefix), rest
    return None, None


def read_secrets():
    """Pull the two OpenSky keys out of secrets.yaml.

    Deliberately a line scan rather than a YAML parse: it avoids depending on
    pyyaml being importable in whatever container this runs in, and the keys are
    simple unquoted scalars.
    """
    want = ("opensky_client_id", "opensky_client_secret")
    out = {}
    with open(SECRETS) as fh:
        for line in fh:
            line = line.strip()
            for k in want:
                if line.startswith(k + ":"):
                    out[k] = line.split(":", 1)[1].strip().strip('"').strip("'")
    missing = [k for k in want if k not in out]
    if missing:
        raise RuntimeError(f"missing from secrets.yaml: {', '.join(missing)}")
    return out["opensky_client_id"], out["opensky_client_secret"]


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def get_token(client_id, client_secret):
    """Return a cached token, refreshing it if missing or near expiry."""
    try:
        with open(CACHE) as fh:
            c = json.load(fh)
        # 120s margin so a token cannot expire mid-request
        if c.get("expires_at", 0) - 120 > time.time() and c.get("token"):
            return c["token"], False
    except (OSError, ValueError):
        pass

    d = _post_form(TOKEN_URL, {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    })
    token = d["access_token"]
    try:
        with open(CACHE, "w") as fh:
            json.dump({"token": token,
                       "expires_at": time.time() + int(d.get("expires_in", 1800))}, fh)
    except OSError:
        pass          # cache is an optimisation, not a requirement
    return token, True


def bbox(lat, lon, radius_m):
    """Convert a centre point + radius in metres to a lat/lon rectangle.

    A degree of latitude is ~111.32 km everywhere; a degree of longitude
    shrinks by cos(latitude). Keeping the box small matters: OpenSky charges
    1 credit up to 25 sq degrees and up to 4 credits above 400.
    """
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def main():
    if len(sys.argv) not in (4, 5):
        print(json.dumps({"count": -1, "error": "usage: LAT LON RADIUS_M [MAX_ALT_M]"}))
        return
    try:
        lat, lon, radius = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
        max_alt = float(sys.argv[4]) if len(sys.argv) == 5 else 0.0
    except ValueError as e:
        print(json.dumps({"count": -1, "error": f"bad argument: {e}"}))
        return

    try:
        client_id, client_secret = read_secrets()
        token, refreshed = get_token(client_id, client_secret)
        lamin, lomin, lamax, lomax = bbox(lat, lon, radius)
        qs = urllib.parse.urlencode({"lamin": f"{lamin:.4f}", "lomin": f"{lomin:.4f}",
                                     "lamax": f"{lamax:.4f}", "lomax": f"{lomax:.4f}"})
        req = urllib.request.Request(f"{STATES_URL}?{qs}",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            credits_left = r.headers.get("x-rate-limit-remaining")
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # 401 usually means a stale cached token; drop it so the next run refreshes
        if e.code == 401:
            try:
                import os
                os.remove(CACHE)
            except OSError:
                pass
        print(json.dumps({"count": -1, "error": f"HTTP {e.code}"}))
        return
    except Exception as e:                      # noqa: BLE001 - must never crash the sensor
        print(json.dumps({"count": -1, "error": f"{type(e).__name__}: {e}"}))
        return

    aircraft = []
    filtered_by_altitude = 0
    for s in (data.get("states") or []):
        try:
            a_lat, a_lon = s[I_LAT], s[I_LON]
            alt = s[I_GEO_ALT]
            if max_alt > 0 and alt is not None and alt > max_alt:
                filtered_by_altitude += 1
                continue
            cs = (s[I_CALLSIGN] or "").strip() or None
            airline, flight_no = resolve_airline(cs)
            aircraft.append({
                "icao24": s[I_ICAO],
                "callsign": cs,
                "airline": airline,
                "flight_number": flight_no,
                "country": s[I_COUNTRY],
                "altitude_m": round(s[I_GEO_ALT]) if s[I_GEO_ALT] is not None else None,
                "velocity_ms": round(s[I_VELOCITY]) if s[I_VELOCITY] is not None else None,
                "heading": round(s[I_TRACK]) if s[I_TRACK] is not None else None,
                "on_ground": s[I_ON_GROUND],
                "distance_km": (round(haversine_km(lat, lon, a_lat, a_lon), 2)
                                if a_lat is not None and a_lon is not None else None),
            })
        except (IndexError, TypeError):
            continue

    aircraft.sort(key=lambda a: (a["distance_km"] is None, a["distance_km"]))
    print(json.dumps({
        "count": len(aircraft),
        "aircraft": aircraft,
        "callsigns": [a["callsign"] for a in aircraft if a["callsign"]],
        "airlines": sorted({a["airline"] for a in aircraft if a["airline"]}),
        "nearest_km": aircraft[0]["distance_km"] if aircraft else None,
        "credits_remaining": int(credits_left) if credits_left and credits_left.isdigit() else None,
        "token_refreshed": refreshed,
        "max_altitude_m": int(max_alt) if max_alt > 0 else None,
        "excluded_above_ceiling": filtered_by_altitude,
        "radius_m": int(radius),
    }))


if __name__ == "__main__":
    main()
