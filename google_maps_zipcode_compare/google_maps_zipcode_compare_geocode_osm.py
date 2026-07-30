#!/usr/bin/env python3
"""Reverse geocode UTM points via OpenStreetMap Nominatim, batch + resumable."""
import csv, json, os, sys, time, urllib.parse, urllib.request

SRC = "/sessions/pensive-nice-faraday/mnt/Downloads/dimona_geocoded_20260730_191925_046924.csv"
CACHE = "/sessions/pensive-nice-faraday/mnt/outputs/osm_cache.json"
UA = "Cowork-geocode/1.0 (sofia.liberman@conifers.ai)"

from pyproj import Transformer
# UTM zone 36N (Israel) -> WGS84
tf = Transformer.from_crs("EPSG:32636", "EPSG:4326", always_xy=True)

def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE) as f: return json.load(f)
    return {}

def save_cache(c):
    with open(CACHE, "w") as f: json.dump(c, f, ensure_ascii=False)

def reverse(lat, lon):
    q = urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "jsonv2",
        "addressdetails": 1, "zoom": 18, "accept-language": "he,en"})
    req = urllib.request.Request("https://nominatim.openstreetmap.org/reverse?"+q,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

# collect unique points
pts = []
seen = set()
with open(SRC, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        x, y = row["utm_x"].strip(), row["utm_y"].strip()
        if not x or not y: continue
        k = (x, y)
        if k in seen: continue
        seen.add(k); pts.append((x, y))

cache = load_cache()
total = len(pts)
done = 0
for i, (x, y) in enumerate(pts):
    key = f"{x},{y}"
    if key in cache:
        done += 1; continue
    lon, lat = tf.transform(float(x), float(y))
    try:
        res = reverse(lat, lon)
        addr = res.get("address", {})
        cache[key] = {
            "lat": round(lat, 7), "lon": round(lon, 7),
            "osm_road": addr.get("road", ""),
            "osm_house": addr.get("house_number", ""),
            "osm_neighbourhood": addr.get("neighbourhood", "") or addr.get("suburb", ""),
            "osm_city": addr.get("city", "") or addr.get("town", "") or addr.get("village", ""),
            "osm_display": res.get("display_name", ""),
        }
    except Exception as e:
        cache[key] = {"lat": round(lat, 7), "lon": round(lon, 7), "error": str(e)}
    done += 1
    if done % 25 == 0:
        save_cache(cache)
        print(f"progress {done}/{total}", flush=True)
    time.sleep(1.1)  # respect Nominatim rate limit

save_cache(cache)
print(f"DONE {done}/{total} unique points cached")
