#!/usr/bin/env python3
"""
unified_engine_comparison.py
=============================
Full three-way comparison of the Dimona municipal zipcode/address list against
GovMap, Google Maps, and OpenStreetMap (Nominatim).
"""
import argparse
import csv
import importlib.util
import json
import math
import os
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import requests

# --- CONFIGURATION ---
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "google_maps_zipcode_compare"

# Updated to use your specified merged CSV file
ZIPCODE_CSV = DATA_DIR / "cleaned_output.csv"
GOVMAP_CACHE_CSV = DATA_DIR / "cleaned_output.csv"

OUTDIR = HERE / "geocode_runs"
OUTPUT_PREFIX = "engine_comparison"

LIMIT = None         # None = full addresses pass. Set to small int for partial pass.
GOVMAP_DELAY = 0.3    # only used if --regeocode-govmap forces a live GovMap pass

def load_env_var(path, var_name):
    """Minimal .env reader — tolerant of stray spaces/quotes around '='."""
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == var_name:
            return val.strip().strip('"').strip("'")
    return ""


ENABLE_GOOGLE = True
GOOGLE_API_KEY_ENV_FILE = HERE / "Google_API_KEY.env"
GOOGLE_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY") or load_env_var(GOOGLE_API_KEY_ENV_FILE, "GOOGLE_MAPS_API_KEY")
GOOGLE_DELAY = 0.05

ENABLE_OSM = True
OSM_DELAY = 1.01      # Nominatim usage policy floor: >= 1.0 req/sec
OSM_CONTACT_EMAIL = "nir.vegh.98@gmail.com"
OSM_USER_AGENT = f"TALPIOT_HAC_2026_Dimona_EngineComparison/1.0 (contact: {OSM_CONTACT_EMAIL})"

GOOGLE_REVERSE_CACHE_JSON = DATA_DIR / "google_cache.json"
OSM_REVERSE_CACHE_JSON = DATA_DIR / "osm_reverse_cache.json"
GOOGLE_FORWARD_CACHE_JSON = DATA_DIR / "google_forward_cache.json"
OSM_FORWARD_CACHE_JSON = DATA_DIR / "osm_forward_cache.json"

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

CACHE_FLUSH_EVERY = 25
ROW_LOG_EVERY = 10


# ---- reuse GovMap's proven geocoding logic from Untitled-1.py ----

def _load_govmap_module():
    spec = importlib.util.spec_from_file_location("govmap_geocode", HERE / "Untitled-1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


govmap = _load_govmap_module()


def make_row_uid(row):
    """Generates a stable unique ID regardless of CSV column naming scheme."""
    if row.get("id"):
        return row["id"].strip()
    loc = row.get("LocationID") or row.get("Location Name") or row.get("city") or ""
    street = row.get("StreetID") or row.get("Street Name") or row.get("street") or ""
    house = row.get("House Number") or row.get("house_number") or ""
    entrance = row.get("Entrance") or row.get("entrance") or "X"
    return f"{str(loc).strip()}-{str(street).strip()}-{str(house).strip()}-{str(entrance).strip()}"


# ---- Hebrew-word-order-tolerant address matching ----

_HEBREW_NIQUD = re.compile(r"[֑-ׇ]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
STREET_STOPWORDS = {"רחוב", "רח", "שדרות", "שד", "סמטה", "סמטת", "דרך", "כביש", "street", "st", "rd", "road"}


def normalize_street_tokens(s):
    s = unicodedata.normalize("NFC", s or "")
    s = _HEBREW_NIQUD.sub("", s)
    s = _PUNCT.sub(" ", s)
    tokens = [t.lower() for t in s.split() if t]
    tokens = [t for t in tokens if t not in STREET_STOPWORDS]
    return tuple(sorted(tokens))


def street_match(a, b):
    ta, tb = normalize_street_tokens(a), normalize_street_tokens(b)
    if not ta or not tb:
        return "none"
    if ta == tb:
        return "exact"
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa or (sa & sb):
        return "partial"
    return "none"


def house_match(a, b):
    da = re.sub(r"\D", "", str(a or "")).lstrip("0")
    db = re.sub(r"\D", "", str(b or "")).lstrip("0")
    if not da or not db:
        return "none"
    return "exact" if da == db else "none"


# ---- point identity ----

def point_key(lat, lon):
    ux, uy = govmap.wgs84_to_utm36n(lat, lon)
    return f"{ux:.2f},{uy:.2f}"


def haversine_free_distance(utm_a, utm_b):
    return math.hypot(utm_a[0] - utm_b[0], utm_a[1] - utm_b[1])


def parse_point_key(key):
    x, y = key.split(",")
    return float(x), float(y)


# ---- Google Maps Geocoding API ----

def google_forward(address, api_key, retries=3, delay=GOOGLE_DELAY):
    if not api_key:
        return {"status": "no_key"}
    print(f"    [Google API] Forward geocoding query: '{address}'", file=sys.stderr)
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(GOOGLE_GEOCODE_URL,
                             params={"address": address, "region": "il", "key": api_key},
                             timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"    [Google API Error] Attempt {attempt}/{retries}: {e}", file=sys.stderr)
            time.sleep(delay * attempt + 1)
            continue
        data = r.json()
        status = data.get("status")
        if status == "OK":
            loc = data["results"][0]["geometry"]["location"]
            return {"status": "OK", "lat": loc["lat"], "lon": loc["lng"],
                    "full": data["results"][0].get("formatted_address", "")}
        if status == "ZERO_RESULTS":
            return {"status": status}
        if status in ("REQUEST_DENIED", "INVALID_REQUEST"):
            return {"status": status, "error": data.get("error_message", "")}
        time.sleep(max(delay * attempt, 1) * 2)
    return {"status": "FAILED"}


def google_reverse(lat, lon, api_key, retries=3, delay=GOOGLE_DELAY):
    if not api_key:
        return {"status": "no_key"}
    print(f"    [Google API] Reverse geocoding query at ({lat:.5f}, {lon:.5f})", file=sys.stderr)
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(GOOGLE_GEOCODE_URL,
                             params={"latlng": f"{lat},{lon}", "language": "he", "key": api_key},
                             timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"    [Google API Error] Attempt {attempt}/{retries}: {e}", file=sys.stderr)
            time.sleep(delay * attempt + 1)
            continue
        data = r.json()
        status = data.get("status")
        if status == "OK" and data.get("results"):
            best = data["results"][0]
            comp = {c["types"][0]: c["long_name"] for c in best.get("address_components", []) if c.get("types")}
            return {"status": "OK", "street": comp.get("route", ""), "house": comp.get("street_number", ""),
                    "city": comp.get("locality", ""), "full": best.get("formatted_address", "")}
        if status == "ZERO_RESULTS":
            return {"status": status}
        time.sleep(max(delay * attempt, 1) * 2)
    return {"status": "FAILED"}


# ---- OSM Nominatim ----

def osm_forward(address, user_agent, retries=3):
    print(f"    [OSM Nominatim] Forward geocoding query: '{address}'", file=sys.stderr)
    for attempt in range(1, retries + 1):
        try:
            r = govmap.osm_geocode(address, user_agent)
            if r.status_code == 429:
                print(f"    [OSM Rate Limit] Attempt {attempt}/{retries}, backing off 30s", file=sys.stderr)
                time.sleep(30)
                continue
            r.raise_for_status()
            data = r.json()
            if data:
                return {"status": "OK", "lat": float(data[0]["lat"]), "lon": float(data[0]["lon"]),
                        "full": data[0].get("display_name", "")}
            return {"status": "ZERO_RESULTS"}
        except requests.RequestException as e:
            print(f"    [OSM Error] Attempt {attempt}/{retries}: {e}", file=sys.stderr)
            time.sleep(2)
    return {"status": "FAILED"}


def osm_reverse(lat, lon, user_agent, retries=3):
    print(f"    [OSM Nominatim] Reverse geocoding query at ({lat:.5f}, {lon:.5f})", file=sys.stderr)
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(NOMINATIM_REVERSE_URL,
                             params={"lat": lat, "lon": lon, "format": "jsonv2",
                                     "addressdetails": 1, "zoom": 18, "accept-language": "he,en"},
                             headers={"User-Agent": user_agent}, timeout=15)
            if r.status_code == 429:
                print(f"    [OSM Rate Limit] Attempt {attempt}/{retries}, backing off 30s", file=sys.stderr)
                time.sleep(30)
                continue
            r.raise_for_status()
            data = r.json()
            a = data.get("address", {})
            if a:
                return {"status": "OK", "street": a.get("road", ""), "house": a.get("house_number", ""),
                        "city": a.get("city") or a.get("town") or a.get("village") or "",
                        "full": data.get("display_name", "")}
            return {"status": "ZERO_RESULTS"}
        except requests.RequestException as e:
            print(f"    [OSM Error] Attempt {attempt}/{retries}: {e}", file=sys.stderr)
            time.sleep(2)
    return {"status": "FAILED"}


# ---- caches ----

def load_json_cache(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def normalize_google_cache_entry(entry):
    if "status" in entry:
        return entry
    if "error" in entry:
        return {"status": "FAILED", "error": entry["error"]}
    return {
        "status": "OK" if entry.get("osm_road") else "ZERO_RESULTS",
        "street": entry.get("osm_road", ""),
        "house": entry.get("osm_house", ""),
        "city": entry.get("osm_city", ""),
        "full": entry.get("osm_display", ""),
    }


# ---- source data helpers ----

def read_govmap_cache(path):
    out = {}
    print(f"Loading cached GovMap results from {path.name}...", file=sys.stderr)
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            entry = {"match_type": row.get("match_type") or row.get("status") or "none"}
            
            ux = row.get("utm_x", "").strip()
            uy = row.get("utm_y", "").strip()
            ix = row.get("itm_x", "").strip()
            iy = row.get("itm_y", "").strip()

            if ux and uy:
                entry["utm_x"], entry["utm_y"] = float(ux), float(uy)
                if ix and iy:
                    lon, lat = govmap.itm_to_wgs84(float(ix), float(iy))
                    entry["lat"], entry["lon"] = lat, lon
            elif ix and iy:
                x_val, y_val = float(ix), float(iy)
                entry["utm_x"], entry["utm_y"] = govmap.itm_to_utm36n(x_val, y_val)
                lon, lat = govmap.itm_to_wgs84(x_val, y_val)
                entry["lat"], entry["lon"] = lat, lon

            row_id = make_row_uid(row)
            out[row_id] = entry
    print(f"Loaded {len(out)} records from GovMap cache.", file=sys.stderr)
    return out


def live_govmap_forward(row, street_cache, delay):
    hit, match_type = govmap.geocode_row(row, street_cache, retries=3, delay=delay)
    if not hit:
        return {"match_type": match_type}
    x, y = hit["x"], hit["y"]
    ux, uy = govmap.itm_to_utm36n(x, y)
    lon, lat = govmap.itm_to_wgs84(x, y)
    return {"match_type": match_type, "utm_x": ux, "utm_y": uy, "lat": lat, "lon": lon}


# ---- main pipeline ----

def run(limit, out_path, regeocode_govmap, enable_google, google_api_key,
        enable_osm, osm_user_agent, resume=True):
    print("=== Initializing Address Geocoding Comparison Pipeline ===", file=sys.stderr)
    if enable_google and not google_api_key:
        print("WARNING: No GOOGLE_MAPS_API_KEY set — Google forward calls will be skipped.", file=sys.stderr)

    google_rev_cache = {k: normalize_google_cache_entry(v) for k, v in load_json_cache(GOOGLE_REVERSE_CACHE_JSON).items()}
    osm_rev_cache = load_json_cache(OSM_REVERSE_CACHE_JSON)
    google_fwd_cache = load_json_cache(GOOGLE_FORWARD_CACHE_JSON)
    osm_fwd_cache = load_json_cache(OSM_FORWARD_CACHE_JSON)

    print(f"Caches loaded — Google Fwd: {len(google_fwd_cache)}, Google Rev: {len(google_rev_cache)}, "
          f"OSM Fwd: {len(osm_fwd_cache)}, OSM Rev: {len(osm_rev_cache)}", file=sys.stderr)

    govmap_by_id = {} if regeocode_govmap else read_govmap_cache(GOVMAP_CACHE_CSV)
    govmap_street_cache = {}

    done_ids = set()
    if resume and out_path.exists():
        with open(out_path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done_ids.add(r["id"])
        print(f"Resuming pipeline: {len(done_ids)} addresses already processed in {out_path.name}", file=sys.stderr)

    with open(ZIPCODE_CSV, newline="", encoding="utf-8-sig") as f_count:
        total_source_rows = sum(1 for _ in csv.DictReader(f_count))
    if limit:
        total_source_rows = min(total_source_rows, limit)

    print(f"Total target records to process: {total_source_rows - len(done_ids)} (out of {total_source_rows})", file=sys.stderr)

    fieldnames = [
        "id", "city", "street", "house_number", "entrance", "zip",
        "govmap_status", "govmap_lat", "govmap_lon",
        "google_fwd_status", "google_fwd_lat", "google_fwd_lon", "google_fwd_full",
        "osm_fwd_status", "osm_fwd_lat", "osm_fwd_lon", "osm_fwd_full",
        "dist_govmap_google_m", "dist_govmap_osm_m", "dist_google_osm_m",
        "google_rev_at_govmap_street", "google_rev_at_govmap_house", "google_rev_at_govmap_match",
        "osm_rev_at_govmap_street", "osm_rev_at_govmap_house", "osm_rev_at_govmap_match",
        "osm_rev_at_google_street", "osm_rev_at_google_house", "osm_rev_at_google_match",
        "google_rev_at_osm_street", "google_rev_at_osm_house", "google_rev_at_osm_match",
        "flags",
    ]

    write_header = not (resume and out_path.exists() and done_ids)
    out_mode = "a" if (resume and out_path.exists() and done_ids) else "w"

    counts = {
        "total": len(done_ids),
        "govmap_missing": 0, "google_fwd_missing": 0, "osm_fwd_missing": 0,
        "google_rev_at_govmap_mismatch": 0, "osm_rev_at_govmap_mismatch": 0,
        "osm_rev_at_google_mismatch": 0, "google_rev_at_osm_mismatch": 0,
    }

    def flush_caches():
        print("  -> Flushing JSON cache files to disk...", file=sys.stderr)
        save_json_cache(GOOGLE_REVERSE_CACHE_JSON, google_rev_cache)
        save_json_cache(OSM_REVERSE_CACHE_JSON, osm_rev_cache)
        save_json_cache(GOOGLE_FORWARD_CACHE_JSON, google_fwd_cache)
        save_json_cache(OSM_FORWARD_CACHE_JSON, osm_fwd_cache)

    with open(ZIPCODE_CSV, newline="", encoding="utf-8-sig") as f_in, \
         open(out_path, out_mode, newline="", encoding="utf-8-sig") as f_out:

        reader = csv.DictReader(f_in)
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        for i, row in enumerate(reader, 1):
            if limit and i > limit:
                break

            city = (row.get("city") or row.get("Location Name") or "").strip()
            street = (row.get("street") or row.get("Street Name") or "").strip()
            house_number = (row.get("house_number") or row.get("House Number") or "").strip().lstrip("0")
            entrance = (row.get("entrance") or row.get("Entrance") or "").strip()
            zip_code = (row.get("zip") or row.get("ZIP 7") or "").strip()

            uid = make_row_uid(row)
            if uid in done_ids:
                continue

            counts["total"] += 1
            pct = (counts["total"] / total_source_rows) * 100 if total_source_rows else 0.0
            label = f"{street} {house_number}, {city}".strip(", ") if street else city

            print(f"[{counts['total']}/{total_source_rows}] ({pct:5.1f}%) Processing ID: {uid} | Label: '{label}'", file=sys.stderr)

            flags = []

            # --- FORWARD: GovMap ---
            if regeocode_govmap:
                g = live_govmap_forward(row, govmap_street_cache, GOVMAP_DELAY)
                time.sleep(GOVMAP_DELAY)
            else:
                g = govmap_by_id.get(uid, {"match_type": "none"})
            govmap_status = g["match_type"]
            govmap_pt = (g["lat"], g["lon"]) if "lat" in g else None
            govmap_utm = (g["utm_x"], g["utm_y"]) if "utm_x" in g else None
            if govmap_pt is None:
                counts["govmap_missing"] += 1
                flags.append("govmap_missing")

            # --- FORWARD: Google ---
            gq = label
            if enable_google:
                if gq not in google_fwd_cache:
                    google_fwd_cache[gq] = google_forward(gq, google_api_key)
                gfwd = google_fwd_cache[gq]
            else:
                gfwd = {"status": "disabled"}
            google_pt = (gfwd["lat"], gfwd["lon"]) if gfwd.get("status") == "OK" else None
            if enable_google and google_pt is None:
                counts["google_fwd_missing"] += 1
                flags.append(f"google_fwd_{gfwd.get('status', 'missing')}")

            # --- FORWARD: OSM ---
            oq = f"{street} {house_number}, {city}" if street else city
            if enable_osm and oq:
                if oq not in osm_fwd_cache:
                    osm_fwd_cache[oq] = osm_forward(oq, osm_user_agent)
                    time.sleep(OSM_DELAY)
                ofwd = osm_fwd_cache[oq]
            else:
                ofwd = {"status": "disabled" if enable_osm else "unknown_street"}
            osm_pt = (ofwd["lat"], ofwd["lon"]) if ofwd.get("status") == "OK" else None
            if enable_osm and osm_pt is None:
                counts["osm_fwd_missing"] += 1
                flags.append(f"osm_fwd_{ofwd.get('status', 'missing')}")

            # --- distances between engines' points ---
            def dist(pa_utm, pb):
                if pa_utm is None or pb is None:
                    return ""
                pb_utm = govmap.wgs84_to_utm36n(pb[0], pb[1])
                return f"{haversine_free_distance(pa_utm, pb_utm):.1f}"

            google_utm = govmap.wgs84_to_utm36n(*google_pt) if google_pt else None
            osm_utm = govmap.wgs84_to_utm36n(*osm_pt) if osm_pt else None

            dist_govmap_google = dist(govmap_utm, google_pt) if govmap_utm else ""
            dist_govmap_osm = dist(govmap_utm, osm_pt) if govmap_utm else ""
            dist_google_osm = f"{haversine_free_distance(google_utm, osm_utm):.1f}" if google_utm and osm_utm else ""

            # --- REVERSE cross-checks ---
            def google_rev_at(pt, utm):
                if pt is None:
                    return {"status": "n/a"}
                key = f"{utm[0]:.2f},{utm[1]:.2f}"
                if key not in google_rev_cache:
                    if not enable_google:
                        return {"status": "disabled"}
                    google_rev_cache[key] = normalize_google_cache_entry(google_reverse(pt[0], pt[1], google_api_key))
                    time.sleep(GOOGLE_DELAY)
                return google_rev_cache[key]

            def osm_rev_at(pt, utm):
                if pt is None:
                    return {"status": "n/a"}
                key = f"{utm[0]:.2f},{utm[1]:.2f}"
                if key not in osm_rev_cache:
                    if not enable_osm:
                        return {"status": "disabled"}
                    osm_rev_cache[key] = osm_reverse(pt[0], pt[1], osm_user_agent)
                    time.sleep(OSM_DELAY)
                return osm_rev_cache[key]

            g_rev_gm = google_rev_at(govmap_pt, govmap_utm) if govmap_utm else {"status": "n/a"}
            o_rev_gm = osm_rev_at(govmap_pt, govmap_utm) if govmap_utm else {"status": "n/a"}

            o_rev_goog = {"status": "n/a"}
            if google_utm and (not govmap_utm or f"{google_utm[0]:.2f},{google_utm[1]:.2f}" != f"{google_utm[0]:.2f},{google_utm[1]:.2f}"):
                o_rev_goog = osm_rev_at(google_pt, google_utm)

            g_rev_osm = {"status": "n/a"}
            if osm_utm and (not govmap_utm or f"{osm_utm[0]:.2f},{osm_utm[1]:.2f}" != f"{govmap_utm[0]:.2f},{govmap_utm[1]:.2f}"):
                g_rev_osm = google_rev_at(osm_pt, osm_utm)

            def match_flag(rev, counter_key):
                if rev.get("status") != "OK":
                    return "n/a"
                sm = street_match(street, rev.get("street", ""))
                hm = house_match(house_number, rev.get("house", "")) if house_number else "n/a"
                verdict = "mismatch" if sm == "none" else ("partial" if sm == "partial" or hm == "none" else "match")
                if verdict == "mismatch":
                    counts[counter_key] += 1
                    flags.append(counter_key)
                return verdict

            g_rev_gm_match = match_flag(g_rev_gm, "google_rev_at_govmap_mismatch")
            o_rev_gm_match = match_flag(o_rev_gm, "osm_rev_at_govmap_mismatch")
            o_rev_goog_match = match_flag(o_rev_goog, "osm_rev_at_google_mismatch")
            g_rev_osm_match = match_flag(g_rev_osm, "google_rev_at_osm_mismatch")

            writer.writerow({
                "id": uid, "city": city, "street": street, "house_number": house_number,
                "entrance": entrance, "zip": zip_code,
                "govmap_status": govmap_status,
                "govmap_lat": f"{govmap_pt[0]:.6f}" if govmap_pt else "",
                "govmap_lon": f"{govmap_pt[1]:.6f}" if govmap_pt else "",
                "google_fwd_status": gfwd.get("status", ""),
                "google_fwd_lat": f"{google_pt[0]:.6f}" if google_pt else "",
                "google_fwd_lon": f"{google_pt[1]:.6f}" if google_pt else "",
                "google_fwd_full": gfwd.get("full", ""),
                "osm_fwd_status": ofwd.get("status", ""),
                "osm_fwd_lat": f"{osm_pt[0]:.6f}" if osm_pt else "",
                "osm_fwd_lon": f"{osm_pt[1]:.6f}" if osm_pt else "",
                "osm_fwd_full": ofwd.get("full", ""),
                "dist_govmap_google_m": dist_govmap_google,
                "dist_govmap_osm_m": dist_govmap_osm,
                "dist_google_osm_m": dist_google_osm,
                "google_rev_at_govmap_street": g_rev_gm.get("street", ""),
                "google_rev_at_govmap_house": g_rev_gm.get("house", ""),
                "google_rev_at_govmap_match": g_rev_gm_match,
                "osm_rev_at_govmap_street": o_rev_gm.get("street", ""),
                "osm_rev_at_govmap_house": o_rev_gm.get("house", ""),
                "osm_rev_at_govmap_match": o_rev_gm_match,
                "osm_rev_at_google_street": o_rev_goog.get("street", ""),
                "osm_rev_at_google_house": o_rev_goog.get("house", ""),
                "osm_rev_at_google_match": o_rev_goog_match,
                "google_rev_at_osm_street": g_rev_osm.get("street", ""),
                "google_rev_at_osm_house": g_rev_osm.get("house", ""),
                "google_rev_at_osm_match": g_rev_osm_match,
                "flags": ";".join(flags),
            })
            f_out.flush()

            if counts["total"] % CACHE_FLUSH_EVERY == 0:
                flush_caches()

            if counts["total"] % ROW_LOG_EVERY == 0:
                print(f"--- [Progress Summary @ {counts['total']}/{total_source_rows}] "
                      f"GovMap Misses: {counts['govmap_missing']} | "
                      f"Google Fwd Misses: {counts['google_fwd_missing']} | "
                      f"OSM Fwd Misses: {counts['osm_fwd_missing']} ---", file=sys.stderr)

    flush_caches()

    print("\n=== Processing Complete ===", file=sys.stderr)
    print(f"Addresses processed: {counts['total']}", file=sys.stderr)
    print(f"GovMap forward missing:  {counts['govmap_missing']}", file=sys.stderr)
    print(f"Google forward missing:  {counts['google_fwd_missing']}", file=sys.stderr)
    print(f"OSM forward missing:     {counts['osm_fwd_missing']}", file=sys.stderr)
    print(f"Google reverse-at-GovMap-point name mismatches: {counts['google_rev_at_govmap_mismatch']}", file=sys.stderr)
    print(f"OSM reverse-at-GovMap-point name mismatches:    {counts['osm_rev_at_govmap_mismatch']}", file=sys.stderr)
    print(f"OSM reverse-at-Google-point name mismatches:    {counts['osm_rev_at_google_mismatch']}", file=sys.stderr)
    print(f"Google reverse-at-OSM-point name mismatches:    {counts['google_rev_at_osm_mismatch']}", file=sys.stderr)
    print(f"\nWrote results to {out_path}", file=sys.stderr)


def unique_run_filename(prefix=OUTPUT_PREFIX):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}.csv"


def main():
    ap = argparse.ArgumentParser(description="Three-way GovMap / Google / OSM address comparison.")
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--outdir", default=str(OUTDIR),
                    help="Folder each run's output CSV is written into.")
    ap.add_argument("--out",
                    help="Output CSV filename.")
    ap.add_argument("--regeocode-govmap", action="store_true",
                    help="Re-query GovMap live instead of reusing GOVMAP_CACHE_CSV.")
    ap.add_argument("--google", dest="google", action="store_true", default=ENABLE_GOOGLE)
    ap.add_argument("--no-google", dest="google", action="store_false")
    ap.add_argument("--osm", dest="osm", action="store_true", default=ENABLE_OSM)
    ap.add_argument("--no-osm", dest="osm", action="store_false")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.out:
        out_path = Path(args.out)
        if not out_path.parent.name and str(out_path.parent) == ".":
            out_path = outdir / out_path
    else:
        out_path = outdir / unique_run_filename()

    run(limit=args.limit, out_path=out_path, regeocode_govmap=args.regeocode_govmap,
        enable_google=args.google, google_api_key=GOOGLE_API_KEY,
        enable_osm=args.osm, osm_user_agent=OSM_USER_AGENT, resume=not args.no_resume)


if __name__ == "__main__":
    main()