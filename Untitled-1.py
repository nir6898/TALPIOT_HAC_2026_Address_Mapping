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
import time
import argparse
from urllib.parse import urlparse, parse_qs

import requests

# --- Endpoints used by the govmap.gov.il front end (es = elasticsearch tier) ---
AUTOCOMPLETE_URL = "https://es.govmap.gov.il/TldSearch/api/AutoComplete"
SEARCH_URL = "https://es.govmap.gov.il/TldSearch/api/DetailsByQuery"

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


def process_csv(input_path, output_path, delay=0.3, limit=None, retries=3, resume=True):
    """Read dimona_zipcodes.csv, geocode every row via GovMap, convert ITM->UTM,
    and write address/id/ITM/UTM/ZIP to output_path. Writes incrementally and can
    resume: rows whose unique ID is already present in an existing output_path
    are skipped."""
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
    fieldnames = ["id", "address", "zip", "match_type", "itm_x", "itm_y", "utm_x", "utm_y"]
    street_cache = {}
    counts = {"exact": 0, "street": 0, "none": 0}

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
            zip5 = row.get("ZIP 5", "").strip()

            hit, match_type = geocode_row(row, street_cache, retries, delay)
            counts[match_type] += 1

            itm_x = itm_y = utm_x = utm_y = ""
            if hit:
                x, y = hit["x"], hit["y"]
                itm_x, itm_y = f"{x:.2f}", f"{y:.2f}"
                ux, uy = itm_to_utm36n(x, y)
                utm_x, utm_y = f"{ux:.2f}", f"{uy:.2f}"
            else:
                print(f"[{i}] no geocode result for '{address}'", file=sys.stderr)

            writer.writerow({
                "id": uid,
                "address": address,
                "zip": zip5,
                "match_type": match_type,
                "itm_x": itm_x,
                "itm_y": itm_y,
                "utm_x": utm_x,
                "utm_y": utm_y,
            })
            f_out.flush()

            if i % 50 == 0:
                print(f"...processed {i} rows "
                      f"(exact={counts['exact']} street={counts['street']} none={counts['none']})",
                      file=sys.stderr)

            time.sleep(delay)

    print(f"Done. Output written to {output_path}. "
          f"exact={counts['exact']} street={counts['street']} none={counts['none']}",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="GovMap Hebrew-address geocoder.")
    ap.add_argument("address", nargs="?", help="Hebrew address to geocode (single lookup mode).")
    ap.add_argument("--url", help="Parse ITM X-Y directly from a govmap share URL.")
    ap.add_argument("--csv", default="dimona_zipcodes.csv",
                    help="Input CSV to batch-geocode (tab-delimited dimona_zipcodes.csv format).")
    ap.add_argument("--out", default="dimona_addresses_geocoded.csv",
                    help="Output CSV path for batch mode.")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="Seconds to sleep between requests in batch mode.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N rows in batch mode (for testing).")
    ap.add_argument("--no-resume", action="store_true",
                    help="Ignore/overwrite any existing --out file instead of resuming.")
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

    # Default: batch-geocode the CSV.
    process_csv(args.csv, args.out, delay=args.delay, limit=args.limit,
                resume=not args.no_resume)


if __name__ == "__main__":
    main()