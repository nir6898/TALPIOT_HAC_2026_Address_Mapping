#!/usr/bin/env python3
"""
unified_engine_comparison.py
=============================
Full three-way comparison of the Dimona municipal zipcode/address list against
GovMap, Google Maps, and OpenStreetMap (Nominatim).

For every source address (from ../dimona_zipcodes.csv) this:

  1. FORWARD  — geocodes the address text -> (lat, lon) via all three engines
                (GovMap is read from an already-completed run to avoid
                re-querying it; see GOVMAP_CACHE_CSV below).
  2. REVERSE  — for every distinct point produced above, asks the *other*
                engines "what address is here?" (GovMap has no public
                reverse-geocoding endpoint we've found, so it's forward-only;
                see the NOTE below).
  3. COMPARE  — computes straight-line distances between the engines' points
                and flags street-name / house-number mismatches, using a
                Hebrew-word-order-tolerant matcher (e.g. "מלכה הנרי" vs
                "הנרי מלכה" — the same street, words swapped — is common
                between GovMap and Google/OSM and would false-flag under a
                naive string comparison).

Output: one row per source address in OUTPUT_CSV, plus a printed summary of
missing addresses / mismatches per engine.

NOTE on GovMap reverse-geocoding: the only GovMap endpoint this project has
reverse-engineered (see ../Untitled-1.py) is address-text search. A genuine
point -> address endpoint may exist on govmap.gov.il (e.g. behind the map's
click-to-identify feature) but discovering it requires inspecting live
DevTools network traffic in a browser, which wasn't available here. So GovMap
only appears in the FORWARD comparison; the REVERSE cross-checks are Google
<-> OSM only.

------------------------------------------------------------------------------
CONFIGURATION — edit directly if running via an IDE's "Run" button (which
invokes this with no CLI arguments); everything below can be overridden with
matching --flags when run from a terminal instead.
------------------------------------------------------------------------------
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
from datetime import datetime
from pathlib import Path

import requests

# --- CONFIGURATION ---
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

ZIPCODE_CSV = REPO_ROOT / "dimona_zipcodes.csv"
GOVMAP_CACHE_CSV = HERE / "google_maps_zipcode_compare_dimona_geocoded_20260730_191925_046924.csv"
OUTPUT_CSV = HERE / "engine_comparison.csv"     # fixed name: this is a long job, resumed across runs

LIMIT = 200          # cap rows for a first pass; set to None for the full ~4,712 addresses
                      # (full run is multi-hour: OSM's Nominatim caps requests at 1/sec, and each
                      # address can need up to 3 forward + 2 reverse OSM calls)
GOVMAP_DELAY = 0.3    # only used if --regeocode-govmap forces a live GovMap pass

ENABLE_GOOGLE = True
GOOGLE_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GOOGLE_DELAY = 0.05

ENABLE_OSM = True
OSM_DELAY = 1.01      # Nominatim usage policy floor: >= 1.0 req/sec
OSM_CONTACT_EMAIL = "nir.vegh.98@gmail.com"
OSM_USER_AGENT = f"TALPIOT_HAC_2026_Dimona_EngineComparison/1.0 (contact: {OSM_CONTACT_EMAIL})"

# Reuses the cache already built by google_maps_zipcode_compare_google_address_match.py
# (683 points already reverse-geocoded via Google — free head start, no key needed to reuse it).
GOOGLE_REVERSE_CACHE_JSON = HERE / "google_cache.json"
OSM_REVERSE_CACHE_JSON = HERE / "osm_reverse_cache.json"
GOOGLE_FORWARD_CACHE_JSON = HERE / "google_forward_cache.json"
OSM_FORWARD_CACHE_JSON = HERE / "osm_forward_cache.json"

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

CACHE_FLUSH_EVERY = 25
ROW_LOG_EVERY = 25


# ---- reuse GovMap's proven geocoding logic from ../Untitled-1.py instead of duplicating it ----

def _load_govmap_module():
    spec = importlib.util.spec_from_file_location("govmap_geocode", REPO_ROOT / "Untitled-1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


govmap = _load_govmap_module()


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
    """'exact' (same token set, order-independent), 'partial' (overlap /
    subset — one side missing a word), or 'none'."""
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


# ---- point identity: everything keyed on UTM 36N (2 decimals = cm precision) ----

def point_key(lat, lon):
    ux, uy = govmap.wgs84_to_utm36n(lat, lon)
    return f"{ux:.2f},{uy:.2f}"


def haversine_free_distance(utm_a, utm_b):
    """utm_a/utm_b: (x, y) tuples on the same projected grid — plain Euclidean
    distance in meters is exact here (UTM is a conformal projection over this
    small an area)."""
    return math.hypot(utm_a[0] - utm_b[0], utm_a[1] - utm_b[1])


def parse_point_key(key):
    x, y = key.split(",")
    return float(x), float(y)


# ---- Google Maps Geocoding API ----

def google_forward(address, api_key, retries=3, delay=GOOGLE_DELAY):
    if not api_key:
        return {"status": "no_key"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(GOOGLE_GEOCODE_URL,
                              params={"address": address, "region": "il", "key": api_key},
                              timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"    Google fwd request error (attempt {attempt}/{retries}): {e}", file=sys.stderr)
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
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(GOOGLE_GEOCODE_URL,
                              params={"latlng": f"{lat},{lon}", "language": "he", "key": api_key},
                              timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"    Google rev request error (attempt {attempt}/{retries}): {e}", file=sys.stderr)
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


# ---- OSM Nominatim (forward via govmap.osm_geocode_with_retry-equivalent logic; reverse is new) ----

def osm_forward(address, user_agent, retries=3):
    """Like govmap.osm_geocode_with_retry(), but keeps the display name too
    (that helper only returns lat/lon/status, discarding it)."""
    for attempt in range(1, retries + 1):
        try:
            r = govmap.osm_geocode(address, user_agent)
            if r.status_code == 429:
                print(f"    Nominatim forward rate-limited (attempt {attempt}/{retries}), backing off 30s",
                      file=sys.stderr)
                time.sleep(30)
                continue
            r.raise_for_status()
            data = r.json()
            if data:
                return {"status": "OK", "lat": float(data[0]["lat"]), "lon": float(data[0]["lon"]),
                         "full": data[0].get("display_name", "")}
            return {"status": "ZERO_RESULTS"}
        except requests.RequestException as e:
            print(f"    Nominatim forward request error (attempt {attempt}/{retries}): {e}", file=sys.stderr)
            time.sleep(2)
    return {"status": "FAILED"}


def osm_reverse(lat, lon, user_agent, retries=3):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(NOMINATIM_REVERSE_URL,
                              params={"lat": lat, "lon": lon, "format": "jsonv2",
                                       "addressdetails": 1, "zoom": 18, "accept-language": "he,en"},
                              headers={"User-Agent": user_agent}, timeout=15)
            if r.status_code == 429:
                print(f"    Nominatim reverse rate-limited (attempt {attempt}/{retries}), backing off 30s",
                      file=sys.stderr)
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
            print(f"    Nominatim reverse request error (attempt {attempt}/{retries}): {e}", file=sys.stderr)
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
    """google_cache.json (from the earlier reverse-geocode-of-GovMap-points run)
    used osm_road/osm_house/osm_display field names even for Google's data
    (copy-pasted from the OSM script). Normalize old and new entries to the
    same shape used elsewhere in this script."""
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


# ---- source data ----

def read_govmap_cache(path):
    """id -> govmap forward-geocode result, from an already-completed run of
    Untitled-1.py (avoids re-querying GovMap for data we already have)."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            entry = {"match_type": row.get("match_type", "none")}
            ux, uy = row.get("utm_x", "").strip(), row.get("utm_y", "").strip()
            if ux and uy:
                entry["utm_x"], entry["utm_y"] = float(ux), float(uy)
                x, y = float(row["itm_x"]), float(row["itm_y"])
                lon, lat = govmap.itm_to_wgs84(x, y)
                entry["lat"], entry["lon"] = lat, lon
            out[row["id"]] = entry
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
    if enable_google and not google_api_key:
        print("No GOOGLE_MAPS_API_KEY set — Google forward calls will be skipped (status=no_key). "
              "Cached reverse-of-GovMap-point results from a prior run will still be used where "
              "available.", file=sys.stderr)

    google_rev_cache = {k: normalize_google_cache_entry(v) for k, v in load_json_cache(GOOGLE_REVERSE_CACHE_JSON).items()}
    osm_rev_cache = load_json_cache(OSM_REVERSE_CACHE_JSON)
    google_fwd_cache = load_json_cache(GOOGLE_FORWARD_CACHE_JSON)
    osm_fwd_cache = load_json_cache(OSM_FORWARD_CACHE_JSON)

    govmap_by_id = {} if regeocode_govmap else read_govmap_cache(GOVMAP_CACHE_CSV)
    govmap_street_cache = {}

    done_ids = set()
    if resume and out_path.exists():
        with open(out_path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done_ids.add(r["id"])
        print(f"Resuming: {len(done_ids)} addresses already in {out_path.name}", file=sys.stderr)

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
        "total": 0,
        "govmap_missing": 0, "google_fwd_missing": 0, "osm_fwd_missing": 0,
        "google_rev_at_govmap_mismatch": 0, "osm_rev_at_govmap_mismatch": 0,
        "osm_rev_at_google_mismatch": 0, "google_rev_at_osm_mismatch": 0,
    }

    def flush_caches():
        save_json_cache(GOOGLE_REVERSE_CACHE_JSON, google_rev_cache)
        save_json_cache(OSM_REVERSE_CACHE_JSON, osm_rev_cache)
        save_json_cache(GOOGLE_FORWARD_CACHE_JSON, google_fwd_cache)
        save_json_cache(OSM_FORWARD_CACHE_JSON, osm_fwd_cache)

    with open(ZIPCODE_CSV, newline="", encoding="utf-8-sig") as f_in, \
         open(out_path, out_mode, newline="", encoding="utf-8-sig") as f_out:

        reader = csv.DictReader(f_in, delimiter="\t")
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        for i, row in enumerate(reader, 1):
            if limit and i > limit:
                break

            uid = govmap.build_unique_id(row)
            if uid in done_ids:
                continue

            city = row["Location Name"].strip()
            street = row["Street Name"].strip()
            house_number = row["House Number"].strip().lstrip("0")
            entrance = (row.get("Entrance") or "").strip()
            zip7 = row.get("ZIP 7", "").strip()
            label = govmap.build_label(row)

            counts["total"] += 1
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
            gq = label  # same address text GovMap matched against, for a fair comparison
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
            oq = govmap.build_osm_query(row)
            if enable_osm and oq is not None:
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

            # --- REVERSE cross-checks (skip re-asking an engine about its own forward point) ---
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

            # at GovMap's point: ask Google + OSM what's there
            g_rev_gm = google_rev_at(govmap_pt, govmap_utm) if govmap_utm else {"status": "n/a"}
            o_rev_gm = osm_rev_at(govmap_pt, govmap_utm) if govmap_utm else {"status": "n/a"}

            # at Google's forward point (if it differs from GovMap's): ask OSM what's there
            o_rev_goog = {"status": "n/a"}
            if google_utm and (not govmap_utm or f"{google_utm[0]:.2f},{google_utm[1]:.2f}" != f"{govmap_utm[0]:.2f},{govmap_utm[1]:.2f}"):
                o_rev_goog = osm_rev_at(google_pt, google_utm)

            # at OSM's forward point (if it differs from GovMap's): ask Google what's there
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
                "entrance": entrance, "zip": zip7,
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

            if i % CACHE_FLUSH_EVERY == 0:
                flush_caches()
            if i % ROW_LOG_EVERY == 0:
                print(f"...{i} rows  govmap_missing={counts['govmap_missing']} "
                      f"google_fwd_missing={counts['google_fwd_missing']} "
                      f"osm_fwd_missing={counts['osm_fwd_missing']}", file=sys.stderr)

    flush_caches()

    print("\n=== Summary ===", file=sys.stderr)
    print(f"Addresses processed: {counts['total']}", file=sys.stderr)
    print(f"GovMap forward missing:  {counts['govmap_missing']}", file=sys.stderr)
    print(f"Google forward missing:  {counts['google_fwd_missing']}", file=sys.stderr)
    print(f"OSM forward missing:     {counts['osm_fwd_missing']}", file=sys.stderr)
    print(f"Google reverse-at-GovMap-point name mismatches: {counts['google_rev_at_govmap_mismatch']}", file=sys.stderr)
    print(f"OSM reverse-at-GovMap-point name mismatches:    {counts['osm_rev_at_govmap_mismatch']}", file=sys.stderr)
    print(f"OSM reverse-at-Google-point name mismatches:    {counts['osm_rev_at_google_mismatch']}", file=sys.stderr)
    print(f"Google reverse-at-OSM-point name mismatches:    {counts['google_rev_at_osm_mismatch']}", file=sys.stderr)
    print(f"\nWrote {out_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Three-way GovMap / Google / OSM address comparison.")
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--out", default=str(OUTPUT_CSV))
    ap.add_argument("--regeocode-govmap", action="store_true",
                    help="Re-query GovMap live instead of reusing GOVMAP_CACHE_CSV.")
    ap.add_argument("--google", dest="google", action="store_true", default=ENABLE_GOOGLE)
    ap.add_argument("--no-google", dest="google", action="store_false")
    ap.add_argument("--osm", dest="osm", action="store_true", default=ENABLE_OSM)
    ap.add_argument("--no-osm", dest="osm", action="store_false")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()

    run(limit=args.limit, out_path=Path(args.out), regeocode_govmap=args.regeocode_govmap,
        enable_google=args.google, google_api_key=GOOGLE_API_KEY,
        enable_osm=args.osm, osm_user_agent=OSM_USER_AGENT, resume=not args.no_resume)


if __name__ == "__main__":
    main()
