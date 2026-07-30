#!/usr/bin/env python3
"""
Google Maps address match for the Dimona geocoded file.

Takes utm_x / utm_y from the source CSV, converts UTM 36N -> WGS84, reverse
geocodes each unique point with the Google Geocoding API, then scores the
Google result against the street + house_number already in the file:

  exact     = Google street (route) matches file street AND Google building
              (street_number) matches file house number
  not_exact = street matches but building number differs / missing
  none      = Google found nothing, errored, or a different street

Output: dimona_google_match.csv  (+ .xlsx if openpyxl is installed)

------------------------------------------------------------------------------
SETUP (run these on your Mac, it has internet):

  pip install pyproj openpyxl requests

  # put your key in an env var so it is never hard-coded / shared:
  export GOOGLE_MAPS_API_KEY="your_key_here"

  # keep this script in the SAME folder as the CSV, then:
  python google_address_match.py

Free tier covers ~40k lookups/month; this run is 683 unique points.
------------------------------------------------------------------------------
"""
import csv, json, os, time
import requests
from pyproj import Transformer

SRC   = "dimona_geocoded_20260730_191925_046924.csv"   # same folder as this script
OUT   = "dimona_google_match.csv"
CACHE = "google_cache.json"
KEY   = os.environ.get("GOOGLE_MAPS_API_KEY", "")

if not KEY:
    raise SystemExit("Set GOOGLE_MAPS_API_KEY env var first (see SETUP at top of file).")

tf = Transformer.from_crs("EPSG:32636", "EPSG:4326", always_xy=True)  # UTM 36N -> WGS84


def norm(s):
    return "".join(ch for ch in (s or "").strip().lower() if ch.isalnum() or ch.isspace()).strip()


def reverse(lat, lon):
    r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                     params={"latlng": f"{lat},{lon}", "language": "he", "key": KEY},
                     timeout=30)
    d = r.json()
    if d.get("status") != "OK" or not d.get("results"):
        return {"osm_road": "", "osm_house": "", "osm_display": d.get("status", "")}
    best = d["results"][0]
    comp = {c["types"][0]: c["long_name"] for c in best.get("address_components", []) if c.get("types")}
    return {
        "osm_road": comp.get("route", ""),
        "osm_house": comp.get("street_number", ""),
        "osm_city": comp.get("locality", ""),
        "osm_display": best.get("formatted_address", ""),
    }


def score(g_road, g_house, f_street, f_house):
    if not g_road:
        return "none"
    if norm(g_road) != norm(f_street) and norm(f_street) not in norm(g_road) \
       and norm(g_road) not in norm(f_street):
        return "none"
    if f_house and g_house and str(f_house).strip() == str(g_house).strip():
        return "exact"
    return "not_exact"


cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

seen, uniq = set(), []
with open(SRC, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        x, y = row["utm_x"].strip(), row["utm_y"].strip()
        if not x or not y:
            continue
        k = (x, y)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((x, y))

print(f"{len(uniq)} unique points to geocode")
for i, (x, y) in enumerate(uniq, 1):
    key = f"{x},{y}"
    if key not in cache:
        lon, lat = tf.transform(float(x), float(y))
        try:
            g = reverse(lat, lon)
            g.update(lat=round(lat, 7), lon=round(lon, 7))
            cache[key] = g
        except Exception as e:
            cache[key] = {"lat": round(lat, 7), "lon": round(lon, 7), "error": str(e)}
        time.sleep(0.05)  # Google allows high QPS; small pause is polite
        if i % 50 == 0:
            json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"  {i}/{len(uniq)}")

json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)

fields = ["id", "city", "file_street", "file_house_number", "file_match_type",
          "utm_x", "utm_y", "lat", "lon",
          "google_street", "google_house_number", "google_full_address", "match_score"]
rows_out = []
with open(SRC, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        x, y = row["utm_x"].strip(), row["utm_y"].strip()
        if not x or not y:
            continue
        c = cache.get(f"{x},{y}", {})
        rows_out.append({
            "id": row.get("id", ""), "city": row.get("city", ""),
            "file_street": row.get("street", ""), "file_house_number": row.get("house_number", ""),
            "file_match_type": row.get("match_type", ""),
            "utm_x": x, "utm_y": y, "lat": c.get("lat", ""), "lon": c.get("lon", ""),
            "google_street": c.get("osm_road", ""), "google_house_number": c.get("osm_house", ""),
            "google_full_address": c.get("osm_display", "") or c.get("error", ""),
            "match_score": score(c.get("osm_road", ""), c.get("osm_house", ""),
                                 row.get("street", ""), row.get("house_number", "")),
        })

with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader(); w.writerows(rows_out)
print(f"wrote {OUT} ({len(rows_out)} rows)")

try:
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "google_match"
    ws.append(fields)
    for r in rows_out:
        ws.append([r[k] for k in fields])
    wb.save(OUT.replace(".csv", ".xlsx"))
    print("wrote", OUT.replace(".csv", ".xlsx"))
except ImportError:
    pass
