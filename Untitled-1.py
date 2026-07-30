#!/usr/bin/env python3
"""
govmap_geocode.py
=================
Resolve a Hebrew address to Israeli Transverse Mercator (ITM / EPSG:2039) X-Y
coordinates using the search API that powers govmap.gov.il, with optional
conversion to WGS84 lat/lon and to true UTM zone 36N.

IMPORTANT — "UTM" vs "ITM"
--------------------------
GovMap returns ITM (New Israel Grid, EPSG:2039), *not* UTM. The map center in a
govmap share URL (the `c=` parameter) and the search API both use ITM eastings
(~120k-320k) and northings (~380k-800k). This script returns the native ITM X-Y
and can additionally convert to UTM 36N (EPSG:32636) or WGS84 if you need them.

Usage
-----
    pip install requests pyproj          # pyproj only needed for conversions
    python govmap_geocode.py "some Hebrew address"

    # or from a govmap URL that already contains the center:
    python govmap_geocode.py --url "https://www.govmap.gov.il/?c=184000,668000&z=10"

If the search endpoint stops returning results, GovMap likely changed it. Open
the site, open DevTools -> Network, search an address, and look at the request
the page fires (AutoComplete / DetailsByQuery). Update SEARCH_URL / AUTOCOMPLETE_URL
below to match. The response parser (_walk_xy) is deliberately shape-agnostic, so
it usually keeps working even if the JSON nesting changes slightly.
"""

import sys
import re
import csv
import json
import math
import time
import uuid
import argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests

# --- CONFIGURATION ---
# These are what actually take effect when you run this file with VSCode's
# ▶ Run button (or `python3 Untitled-1.py` with no arguments) — that path
# invokes main() with an empty argv, so CLI flags like --osm never apply.
# Edit these directly for that workflow; the CLI flags below only matter if
# you're invoking the script from a terminal with explicit arguments.
BATCH_CSV = "dimona_zipcodes.csv"
BATCH_OUTDIR = "geocode_runs"
BATCH_DELAY = 0.3            # seconds between GovMap requests
ENABLE_OSM = True            # cross-check every row against OSM Nominatim too
OSM_DELAY = 1.01             # seconds between Nominatim requests (policy min: 1.0)

# --- Endpoints used by the govmap.gov.il front end (es = elasticsearch tier) ---
AUTOCOMPLETE_URL = "https://es.govmap.gov.il/TldSearch/api/AutoComplete"
SEARCH_URL = "https://es.govmap.gov.il/TldSearch/api/DetailsByQuery"

# OpenStreetMap's free Nominatim geocoder, used to cross-check GovMap's
# results (same API/usage pattern as Create_LLA_from_addresses.py: no key
# needed, but a real identifying User-Agent and a strict 1 req/sec cap are
# required by Nominatim's usage policy — https://operations.osmfoundation.org/policies/nominatim/).
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_USER_AGENT = "TALPIOT_HAC_2026_Dimona_AddressMapping"

# Bitmask selecting which search layers to query. This value = "all layers" as
# used by the site (addresses, parcels, settlements, streets, ...).
DEFAULT_LAYERS = "276267023"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.govmap.gov.il/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he,en;q=0.8",
}


def autocomplete(query, layers=DEFAULT_LAYERS, timeout=15):
    """Return raw autocomplete suggestions for a (possibly partial) Hebrew query.
    requests URL-encodes the Hebrew `query` automatically."""
    params = {"query": query, "ids": layers, "gid": "govmap"}
    r = requests.get(AUTOCOMPLETE_URL, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _walk_xy(obj):
    """Recursively yield (x, y, label) from any nested dict/list that carries an
    X and Y that look like plausible ITM coordinates. Being shape-agnostic makes
    this robust to minor changes in the API's JSON structure."""
    if isinstance(obj, dict):
        keys = {k.lower(): k for k in obj.keys()}
        if "x" in keys and "y" in keys:
            try:
                x = float(obj[keys["x"]])
                y = float(obj[keys["y"]])
                # sanity-check the ITM envelope for Israel
                if 100_000 < x < 340_000 and 350_000 < y < 850_000:
                    label = None
                    for lk in ("value", "resultlable", "resultlabel",
                               "text", "address", "name", "key"):
                        if lk in keys and obj[keys[lk]]:
                            label = obj[keys[lk]]
                            break
                    yield x, y, label
            except (TypeError, ValueError):
                pass
        for v in obj.values():
            yield from _walk_xy(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_xy(v)


def geocode(address, layers=DEFAULT_LAYERS, timeout=15, raw=False):
    """Resolve a Hebrew address to ITM X-Y via GovMap's DetailsByQuery endpoint.

    Returns a de-duplicated list of {"x", "y", "label"} dicts. With raw=True,
    returns (results, full_json_payload) so you can inspect / adapt the parser.
    """
    params = {"query": address, "lyrs": layers, "gid": "govmap"}
    r = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    payload = r.json()

    results, seen = [], set()
    for x, y, label in _walk_xy(payload):
        key = (round(x, 2), round(y, 2))
        if key not in seen:
            seen.add(key)
            results.append({"x": x, "y": y, "label": label})

    return (results, payload) if raw else results


def parse_govmap_url(url):
    """Extract ITM X-Y from a govmap.gov.il share URL. The site stores the map
    center in the `c` query parameter as 'easting,northing'."""
    q = parse_qs(urlparse(url).query)
    if "c" in q:
        parts = re.split(r"[,\s]+", q["c"][0].strip())
        if len(parts) >= 2:
            return float(parts[0]), float(parts[1])
    raise ValueError("No `c=easting,northing` parameter found in the URL.")


# ---- optional coordinate conversions (require: pip install pyproj) ----

def itm_to_wgs84(x, y):
    """ITM (EPSG:2039) -> (lon, lat) in WGS84 (EPSG:4326)."""
    from pyproj import Transformer
    return Transformer.from_crs(2039, 4326, always_xy=True).transform(x, y)


def itm_to_utm36n(x, y):
    """ITM (EPSG:2039) -> UTM zone 36N (EPSG:32636) easting/northing."""
    from pyproj import Transformer
    return Transformer.from_crs(2039, 32636, always_xy=True).transform(x, y)


def wgs84_to_utm36n(lat, lon):
    """WGS84 (EPSG:4326) lat/lon -> UTM zone 36N (EPSG:32636) easting/northing.
    Used to put OSM's lat/lon results on the same grid as GovMap's ITM->UTM
    output so the two sources can be compared directly."""
    from pyproj import Transformer
    return Transformer.from_crs(4326, 32636, always_xy=True).transform(lon, lat)


# ---- OpenStreetMap Nominatim geocoder (free, no key — see Create_LLA_from_addresses.py) ----

def osm_geocode(address, user_agent=OSM_USER_AGENT, country_codes="il", timeout=15):
    """Single call to Nominatim's /search endpoint. Returns the raw HTTP response
    (not .json()) so callers can inspect status_code, e.g. for 429 handling."""
    params = {"q": address, "format": "json", "limit": 1, "countrycodes": country_codes}
    headers = {"User-Agent": user_agent}
    return requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=timeout)


def osm_geocode_with_retry(address, user_agent=OSM_USER_AGENT, retries=3):
    """osm_geocode() wrapped with retries. Returns (lat, lon, status); lat/lon
    are None if nothing was found. Mirrors Create_LLA_from_addresses.py:
    on HTTP 429 it backs off 30s (Nominatim's own recommended cooldown) and
    retries rather than treating it as a normal failure."""
    for attempt in range(1, retries + 1):
        try:
            r = osm_geocode(address, user_agent)
            if r.status_code == 429:
                print(f"  Nominatim rate-limited for '{address}' (attempt {attempt}/{retries}), "
                      f"backing off 30s", file=sys.stderr)
                time.sleep(30)
                continue
            r.raise_for_status()
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"]), "OK"
            return None, None, "ZERO_RESULTS"
        except requests.RequestException as e:
            print(f"  Nominatim request error for '{address}' (attempt {attempt}/{retries}): {e}",
                  file=sys.stderr)
            time.sleep(2)

    return None, None, "FAILED"


def _print_conversions(x, y, indent="     "):
    try:
        lon, lat = itm_to_wgs84(x, y)
        ux, uy = itm_to_utm36n(x, y)
        print(f"{indent}WGS84   lat={lat:.6f}  lon={lon:.6f}")
        print(f"{indent}UTM 36N E={ux:.2f}  N={uy:.2f}")
    except ImportError:
        print(f"{indent}(install pyproj for lat/lon and UTM conversion)")


# ---- batch CSV geocoding (dimona_zipcodes.csv) ----

# House numbers in the source data often carry the entrance letter glued on
# (e.g. "61א"). GovMap's address DB indexes plain numbers only, so a glued
# entrance letter makes an otherwise-real address invisible to the API.
UNKNOWN_STREET_MARKERS = {"", "?"}


def build_house_query(row):
    """House number for the geocode query, without the entrance letter
    (GovMap doesn't index numbers with a glued-on entrance letter)."""
    return row["House Number"].strip().lstrip("0")


def build_label(row):
    """Human-readable full address (street, house+entrance, city) for the
    output CSV — this is what the row *represents*, independent of what
    query string actually found a match."""
    street = row["Street Name"].strip()
    house = row["House Number"].strip().lstrip("0")
    entrance = (row.get("Entrance") or "").strip()
    city = row["Location Name"].strip()

    parts = [street]
    if house:
        parts.append(house + entrance)
    return f"{' '.join(parts)}, {city}"


def build_osm_query(row):
    """Address string to send to Nominatim (the `countrycodes=il` param already
    restricts results to Israel, so no country suffix is needed here)."""
    street = row["Street Name"].strip()
    if street in UNKNOWN_STREET_MARKERS:
        return None
    house = row["House Number"].strip().lstrip("0")
    entrance = (row.get("Entrance") or "").strip()
    city = row["Location Name"].strip()

    parts = [street]
    if house:
        parts.append(house + entrance)
    return f"{' '.join(parts)}, {city}"


def build_unique_id(row):
    """Deterministic ID derived from the source key columns, stable across runs."""
    entrance = (row.get("Entrance") or "").strip()
    return f"{row['LocationID'].strip()}-{row['StreetID'].strip()}-{row['House Number'].strip()}-{entrance or 'X'}"


def geocode_with_retry(query, retries, delay):
    """geocode() wrapped with retries on transient request errors. Returns a
    (possibly empty) hit list; never raises."""
    for attempt in range(1, retries + 1):
        try:
            return geocode(query)
        except requests.RequestException as e:
            print(f"  request error for '{query}' (attempt {attempt}/{retries}): {e}",
                  file=sys.stderr)
            time.sleep(delay * attempt)
    return []


def geocode_row(row, street_cache, retries, delay):
    """Multi-tier geocode for one CSV row:
      1. exact "<street> <house>, <city>"  -> match_type="exact"
      2. street-only "<street>, <city>"     -> match_type="street" (approximate,
         used as a fallback when GovMap's address DB lacks that exact house
         number even though the street itself is mapped)
      3. nothing found / street unknown     -> match_type="none"
    Street-level fallback results are cached per (city, street) since many
    house numbers under the same street all fall back to the same point.
    """
    street = row["Street Name"].strip()
    city = row["Location Name"].strip()
    house = build_house_query(row)

    if street in UNKNOWN_STREET_MARKERS:
        return None, "none"

    if house:
        hits = geocode_with_retry(f"{street} {house}, {city}", retries, delay)
        if hits:
            return hits[0], "exact"
        time.sleep(delay)

    cache_key = (city, street)
    if cache_key in street_cache:
        return street_cache[cache_key], "street" if street_cache[cache_key] else "none"

    hits = geocode_with_retry(f"{street}, {city}", retries, delay)
    hit = hits[0] if hits else None
    street_cache[cache_key] = hit
    return hit, "street" if hit else "none"


def process_csv(input_path, output_path, delay=0.3, limit=None, retries=3, resume=True,
                 osm_enabled=False, osm_delay=1.01, osm_retries=3, osm_user_agent=OSM_USER_AGENT):
    """Read dimona_zipcodes.csv, geocode every row via GovMap, convert ITM->UTM,
    and write address/id/ITM/UTM/ZIP to output_path. Writes incrementally and can
    resume: rows whose unique ID is already present in an existing output_path
    are skipped.

    If osm_enabled, each row is also geocoded via OpenStreetMap's Nominatim
    (free, no key — same API used by Create_LLA_from_addresses.py), converted
    to the same UTM 36N grid, and compared against the GovMap result
    (utm_diff_m = straight-line distance between the two). Nominatim's usage
    policy caps requests at 1/sec, so osm_delay must stay >= 1.0."""
    done_ids = set()
    write_header = True
    if resume:
        try:
            with open(output_path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    done_ids.add(r["id"])
            write_header = not done_ids
        except FileNotFoundError:
            pass

    out_mode = "a" if done_ids else "w"
    fieldnames = ["id", "address", "city", "street", "house_number", "entrance", "zip",
                  "match_type", "itm_x", "itm_y", "lat", "lon", "utm_x", "utm_y",
                  "osm_status", "osm_lat", "osm_lon",
                  "osm_utm_x", "osm_utm_y", "utm_diff_m"]
    street_cache = {}
    counts = {"exact": 0, "street": 0, "none": 0}
    osm_counts = {"OK": 0, "other": 0}

    with open(input_path, newline="", encoding="utf-8-sig") as f_in, \
         open(output_path, out_mode, newline="", encoding="utf-8-sig") as f_out:

        reader = csv.DictReader(f_in, delimiter="\t")
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        for i, row in enumerate(reader, 1):
            if limit and i > limit:
                break

            uid = build_unique_id(row)
            if uid in done_ids:
                continue

            address = build_label(row)
            city = row["Location Name"].strip()
            street = row["Street Name"].strip()
            house_number = row["House Number"].strip().lstrip("0")
            entrance = (row.get("Entrance") or "").strip()
            zip7 = row.get("ZIP 7", "").strip()

            hit, match_type = geocode_row(row, street_cache, retries, delay)
            counts[match_type] += 1

            itm_x = itm_y = lat_s = lon_s = utm_x = utm_y = ""
            govmap_utm = None
            if hit:
                x, y = hit["x"], hit["y"]
                itm_x, itm_y = f"{x:.2f}", f"{y:.2f}"
                lon, lat = itm_to_wgs84(x, y)
                lat_s, lon_s = f"{lat:.6f}", f"{lon:.6f}"
                ux, uy = itm_to_utm36n(x, y)
                utm_x, utm_y = f"{ux:.2f}", f"{uy:.2f}"
                govmap_utm = (ux, uy)
            else:
                print(f"[{i}] no geocode result for '{address}'", file=sys.stderr)

            osm_status = "skipped"
            osm_lat = osm_lon = osm_utm_x = osm_utm_y = utm_diff_m = ""
            if osm_enabled:
                oquery = build_osm_query(row)
                if oquery is None:
                    osm_status = "unknown_street"
                else:
                    lat, lon, osm_status = osm_geocode_with_retry(oquery, osm_user_agent, osm_retries)
                    osm_counts["OK" if osm_status == "OK" else "other"] += 1
                    if lat is not None:
                        osm_lat, osm_lon = f"{lat:.6f}", f"{lon:.6f}"
                        oux, ouy = wgs84_to_utm36n(lat, lon)
                        osm_utm_x, osm_utm_y = f"{oux:.2f}", f"{ouy:.2f}"
                        if govmap_utm:
                            diff = math.hypot(oux - govmap_utm[0], ouy - govmap_utm[1])
                            utm_diff_m = f"{diff:.2f}"
                    else:
                        print(f"[{i}] OSM status={osm_status} for '{oquery}'", file=sys.stderr)
                # Nominatim's usage policy strictly requires >=1 req/sec pacing.
                time.sleep(osm_delay)

            writer.writerow({
                "id": uid,
                "address": address,
                "city": city,
                "street": street,
                "house_number": house_number,
                "entrance": entrance,
                "zip": zip7,
                "match_type": match_type,
                "itm_x": itm_x,
                "itm_y": itm_y,
                "lat": lat_s,
                "lon": lon_s,
                "utm_x": utm_x,
                "utm_y": utm_y,
                "osm_status": osm_status,
                "osm_lat": osm_lat,
                "osm_lon": osm_lon,
                "osm_utm_x": osm_utm_x,
                "osm_utm_y": osm_utm_y,
                "utm_diff_m": utm_diff_m,
            })
            f_out.flush()

            if i % 50 == 0:
                msg = (f"...processed {i} rows "
                       f"(exact={counts['exact']} street={counts['street']} none={counts['none']})")
                if osm_enabled:
                    msg += f"  osm_ok={osm_counts['OK']} osm_other={osm_counts['other']}"
                print(msg, file=sys.stderr)

            time.sleep(delay)

    summary = (f"Done. Output written to {output_path}. "
               f"exact={counts['exact']} street={counts['street']} none={counts['none']}")
    if osm_enabled:
        summary += f"  osm_ok={osm_counts['OK']} osm_other={osm_counts['other']}"
    print(summary, file=sys.stderr)


def unique_run_filename(prefix="dimona_geocoded"):
    """A filename that's unique per run: timestamp + a short random suffix so
    two runs started in the same second never collide."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}.csv"


def main():
    ap = argparse.ArgumentParser(description="GovMap Hebrew-address geocoder.")
    ap.add_argument("address", nargs="?", help="Hebrew address to geocode (single lookup mode).")
    ap.add_argument("--url", help="Parse ITM X-Y directly from a govmap share URL.")
    ap.add_argument("--csv", default=BATCH_CSV,
                    help="Input CSV to batch-geocode (tab-delimited dimona_zipcodes.csv format).")
    ap.add_argument("--outdir", default=BATCH_OUTDIR,
                    help="Folder each batch run's output CSV is written into.")
    ap.add_argument("--out",
                    help="Output CSV filename for batch mode (written inside --outdir unless it's "
                         "an absolute/relative path of its own). Default: an auto-generated unique "
                         "name per run, e.g. dimona_geocoded_20260730_184230_a1b2c3.csv")
    ap.add_argument("--delay", type=float, default=BATCH_DELAY,
                    help="Seconds to sleep between requests in batch mode.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N rows in batch mode (for testing).")
    ap.add_argument("--no-resume", action="store_true",
                    help="Ignore/overwrite any existing --out file instead of resuming.")
    ap.add_argument("--osm", dest="osm", action="store_true", default=ENABLE_OSM,
                    help="Also cross-check every row against OpenStreetMap's Nominatim geocoder "
                         "(free, no key). Adds osm_lat/osm_lon/osm_utm_x/osm_utm_y/utm_diff_m "
                         "columns. Nominatim caps requests at 1/sec, so this roughly doubles the "
                         f"runtime of a full batch. Currently defaults to {ENABLE_OSM} via the "
                         "ENABLE_OSM constant at the top of the file.")
    ap.add_argument("--no-osm", dest="osm", action="store_false",
                    help="Disable the OSM comparison even if ENABLE_OSM is True.")
    ap.add_argument("--osm-delay", type=float, default=OSM_DELAY,
                    help="Seconds to sleep between Nominatim requests. Nominatim's usage policy "
                         "requires >=1.0 — don't lower this below that.")
    ap.add_argument("--osm-user-agent", default=OSM_USER_AGENT,
                    help="User-Agent sent to Nominatim, identifying this script per their usage "
                         "policy (should ideally include contact info for heavy use).")
    args = ap.parse_args()

    if args.url:
        x, y = parse_govmap_url(args.url)
        print(f"From URL:  ITM X={x:.2f}  Y={y:.2f}")
        _print_conversions(x, y)
        return

    if args.address:
        print(f"Geocoding: {args.address}\n")
        hits = geocode(args.address)
        if not hits:
            print("No results parsed. Raw payload (adapt the parser if needed):\n")
            _, payload = geocode(args.address, raw=True)
            print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
            sys.exit(1)

        for i, h in enumerate(hits, 1):
            line = f"{i}. ITM  X={h['x']:.2f}  Y={h['y']:.2f}"
            if h["label"]:
                line += f"   [{h['label']}]"
            print(line)
            _print_conversions(h["x"], h["y"])
        return

    # Default: batch-geocode the CSV, writing a uniquely-named file per run
    # into --outdir so repeated runs never clobber each other.
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.out:
        out_path = Path(args.out)
        if not out_path.parent.name and str(out_path.parent) == ".":
            out_path = outdir / out_path
    else:
        out_path = outdir / unique_run_filename()

    if args.osm:
        if args.osm_delay < 1.0:
            sys.exit("--osm-delay must be >= 1.0 (Nominatim's usage policy caps requests at 1/sec).")
        print("OSM comparison enabled (Nominatim, free). Requests are paced at "
              f"{args.osm_delay}s each per Nominatim's usage policy — a full run will take a while.",
              file=sys.stderr)

    process_csv(args.csv, str(out_path), delay=args.delay, limit=args.limit,
                resume=not args.no_resume, osm_enabled=args.osm,
                osm_delay=args.osm_delay, osm_user_agent=args.osm_user_agent)


if __name__ == "__main__":
    main()