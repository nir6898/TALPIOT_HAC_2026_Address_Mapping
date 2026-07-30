#!/usr/bin/env python3
"""
OSM address match for Dimona geocoded file.

Reads the source CSV, takes utm_x / utm_y, converts UTM 36N -> WGS84,
reverse-geocodes each unique point via OpenStreetMap Nominatim, then scores
the OSM result against the street + house_number already in the file:

  exact     = OSM road matches file street AND OSM house number matches file house number
  not_exact = OSM road matches file street but house number differs / missing
  none      = OSM found nothing, errored, or a different street

Output: dimona_osm_match.csv (and .xlsx if openpyxl installed)

Run locally (needs internet):
    pip install pyproj openpyxl
    python osm_address_match.py

Nominatim usage policy: max 1 request/sec, valid User-Agent. Set your email below.
"""
import csv, json, os, time, urllib.parse, urllib.request

SRC   = "dimona_geocoded_20260730_191925_046924.csv"   # same folder as this script
OUT   = "dimona_osm_match.csv"
CACHE = "osm_cache.json"
EMAIL = "sofia.liberman@conifers.ai"                    # <- required by Nominatim
UA    = f"dimona-osm-match/1.0 ({EMAIL})"

from pyproj import Transformer
tf = Transformer.from_crs("EPSG:32636", "EPSG:4326", always_xy=True)  # UTM 36N -> WGS84


def norm(s):
    return "".join(ch for ch in (s or "").strip().lower() if ch.isalnum() or ch.isspace()).strip()


def reverse(lat, lon):
    q = urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "jsonv2",
        "addressdetails": 1, "zoom": 18, "accept-language": "he,en"})
    req = urllib.request.Request(
        "https://nominatim.openstreetmap.org/reverse?" + q,
        headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def score(osm_road, osm_house, f_street, f_house):
    if not osm_road:
        return "none"
    if norm(osm_road) != norm(f_street) and norm(f_street) not in norm(osm_road) \
       and norm(osm_road) not in norm(f_street):
        return "none"
    # street matches
    if f_house and osm_house and str(f_house).strip() == str(osm_house).strip():
        return "exact"
    return "not_exact"


# load / resume cache
cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

# collect unique points
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
        uniq.append((x, y, row.get("street", ""), row.get("house_number", "")))

print(f"{len(uniq)} unique points to geocode")

for i, (x, y, f_street, f_house) in enumerate(uniq, 1):
    key = f"{x},{y}"
    if key not in cache:
        lon, lat = tf.transform(float(x), float(y))
        try:
            res = reverse(lat, lon)
            a = res.get("address", {})
            cache[key] = {
                "lat": round(lat, 7), "lon": round(lon, 7),
                "osm_road": a.get("road", ""),
                "osm_house": a.get("house_number", ""),
                "osm_city": a.get("city") or a.get("town") or a.get("village") or "",
                "osm_display": res.get("display_name", ""),
            }
        except Exception as e:
            cache[key] = {"lat": round(lat, 7), "lon": round(lon, 7), "error": str(e)}
        time.sleep(1.1)  # rate limit
        if i % 25 == 0:
            json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"  {i}/{len(uniq)}")

json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)

# write output at row level (every original row that has utm)
fields = ["id", "city", "file_street", "file_house_number", "file_match_type",
          "utm_x", "utm_y", "lat", "lon",
          "osm_street", "osm_house_number", "osm_full_address", "match_score"]
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
            "osm_street": c.get("osm_road", ""), "osm_house_number": c.get("osm_house", ""),
            "osm_full_address": c.get("osm_display", "") or c.get("error", ""),
            "match_score": score(c.get("osm_road", ""), c.get("osm_house", ""),
                                 row.get("street", ""), row.get("house_number", "")),
        })

with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader(); w.writerows(rows_out)
print(f"wrote {OUT} ({len(rows_out)} rows)")

try:
    import openpyxl
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "osm_match"
    ws.append(fields)
    for r in rows_out:
        ws.append([r[k] for k in fields])
    wb.save(OUT.replace(".csv", ".xlsx"))
    print("wrote", OUT.replace(".csv", ".xlsx"))
except ImportError:
    pass
