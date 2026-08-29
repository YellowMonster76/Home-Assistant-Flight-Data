#!/usr/bin/env python3
"""Read back the recent overhead-flight sightings for sensor.plane_log.

The log is written by bin/aerodatabox_lookup.py, one JSON object per line, which
runs exactly once per new qualifying aircraft. This only reads it -- keeping the
writer and reader separate means a malformed line can never stop the sensor
updating.

Prints JSON with a "flights" list, newest first, plus counts for today.
"""
import json
import os
import sys
import time

LOG = "/config/.plane_log.jsonl"


def main():
    limit = 15
    if len(sys.argv) > 1:
        try:
            limit = max(1, min(50, int(sys.argv[1])))
        except ValueError:
            pass

    rows = []
    try:
        with open(LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue          # skip a corrupt line rather than fail
    except OSError:
        rows = []

    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)

    # midnight local, for a "today" count
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    today = sum(1 for r in rows if r.get("ts", 0) >= midnight)

    out = []
    for r in rows[:limit]:
        ts = r.get("ts", 0)
        out.append({
            "time": time.strftime("%H:%M", time.localtime(ts)),
            "date": time.strftime("%d %b", time.localtime(ts)),
            "callsign": r.get("callsign"),
            "airline": r.get("airline"),
            "model": r.get("model"),
            "origin": r.get("origin"),
            "destination": r.get("destination"),
            "altitude_m": r.get("altitude_m"),
            "distance_km": r.get("distance_km"),
        })

    print(json.dumps({
        "count": len(out),
        "today": today,
        "total_logged": len(rows),
        "flights": out,
        "last": (out[0]["callsign"] if out else None),
    }))


if __name__ == "__main__":
    main()
