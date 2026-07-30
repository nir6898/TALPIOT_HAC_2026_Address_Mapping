#!/usr/bin/env python3
"""
generate_interactive_mismatch_map.py
====================================
Generates an interactive Folium map with:
- Dynamic connector line updates (lines hide/show based on connected active shapes)
- Logarithmic min/max distance threshold sliders
- Shape legend for engines
- Interactive checkboxes to show/hide specific engines and connector lines
- Exclusion of addresses containing '?'
"""

import csv
import json
import math
import sys
import matplotlib.cm as cm
import matplotlib.colors as mcolors
from pathlib import Path
import folium

# --- CONFIGURATION ---
INPUT_CSV = Path("unified2\engine_comparison_20260731_013214_92188c.csv")  # Change to your input path
OUTPUT_HTML = Path("dimona_report.html")
TOP_N = 100

DIMONA_CENTER_LAT = 31.0700
DIMONA_CENTER_LON = 35.0350


def safe_float(val):
    try:
        return float(val) if val and val.strip() else None
    except (ValueError, TypeError):
        return None


def get_color_hex(rank_ratio):
    """
    Map rank ratio (0.0 = rank 1 / worst, 1.0 = rank 100) to a color gradient.
    Red -> Orange -> Yellow -> Cyan -> Blue
    """
    cmap = cm.get_cmap("jet_r")  # 'jet_r' maps 0.0 to Red and 1.0 to Blue
    rgba = cmap(rank_ratio)
    return mcolors.to_hex(rgba)


def main():
    if not INPUT_CSV.exists():
        print(f"Error: Could not find input file '{INPUT_CSV}'. Please check the path.", file=sys.stderr)
        sys.exit(1)

    records = []

    with open(INPUT_CSV, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            address = row.get("address", "")
            street = row.get("street", "")
            house_num = row.get("house_number", "")
            city = row.get("city", "")

            # Exclude records containing '?'
            if "?" in address or "?" in street or "?" in house_num:
                continue

            d_gov_goog = safe_float(row.get("dist_govmap_google_m")) or 0.0
            d_gov_osm = safe_float(row.get("dist_govmap_osm_m")) or 0.0
            d_goog_osm = safe_float(row.get("dist_google_osm_m")) or 0.0

            max_dist = max(d_gov_goog, d_gov_osm, d_goog_osm)

            formatted_addr = address.strip() if address.strip() else f"{street} {house_num}, {city}".strip()

            if max_dist > 0:
                records.append({
                    "id": row.get("id", ""),
                    "address": formatted_addr,
                    "max_dist": max_dist,
                    "d_gov_goog": d_gov_goog,
                    "d_gov_osm": d_gov_osm,
                    "d_goog_osm": d_goog_osm,
                    "flags": row.get("flags", ""),
                    "govmap_pt": [safe_float(row.get("govmap_lat")), safe_float(row.get("govmap_lon"))],
                    "google_pt": [safe_float(row.get("google_fwd_lat")), safe_float(row.get("google_fwd_lon"))],
                    "osm_pt": [safe_float(row.get("osm_fwd_lat")), safe_float(row.get("osm_fwd_lon"))],
                })

    # Sort descending by max spatial discrepancy
    records.sort(key=lambda x: x["max_dist"], reverse=True)
    top_records = records[:TOP_N]

    if not top_records:
        print("No valid spatial records found in CSV.", file=sys.stderr)
        sys.exit(1)

    max_error_val = top_records[0]["max_dist"]
    min_error_val = top_records[-1]["max_dist"]

    # Compute logarithmic slider boundaries
    log_min = math.log10(max(1.0, min_error_val))
    log_max = math.log10(max(1.1, max_error_val))

    print(f"Loaded {len(records)} valid records. Processing top {len(top_records)} mismatches "
          f"(Errors: {min_error_val:.1f}m to {max_error_val:.1f}m).")

    # Initialize Leaflet map
    m = folium.Map(
        location=[DIMONA_CENTER_LAT, DIMONA_CENTER_LON],
        zoom_start=14,
        tiles="OpenStreetMap"
    )

    js_records = []

    # Map each record to leaflet markers
    for rank, item in enumerate(top_records, start=1):
        ratio = (rank - 1) / max(1, (len(top_records) - 1))
        color_hex = get_color_hex(ratio)

        popup_html = f"""
        <div style="font-family: Arial, sans-serif; width: 250px;">
            <h4 style="margin:0 0 5px 0;">Rank #{rank} — Max Error: {item['max_dist']:.1f}m</h4>
            <b>Address:</b> {item['address']}<br/>
            <b>ID:</b> {item['id']}<br/><hr style="margin:5px 0;"/>
            <b>Distances:</b><br/>
            • GovMap ↔ Google: {item['d_gov_goog']:.1f} m<br/>
            • GovMap ↔ OSM: {item['d_gov_osm']:.1f} m<br/>
            • Google ↔ OSM: {item['d_goog_osm']:.1f} m<br/><br/>
            <b>Flags:</b> <span style="color: red; font-size: 11px;">{item['flags']}</span>
        </div>
        """

        has_govmap = None not in item["govmap_pt"]
        has_google = None not in item["google_pt"]
        has_osm = None not in item["osm_pt"]

        js_records.append({
            "rank": rank,
            "dist": round(item["max_dist"], 1),
            "hasGovmap": has_govmap,
            "hasGoogle": has_google,
            "hasOsm": has_osm
        })

        # 1. GovMap Marker (Circle)
        if has_govmap:
            folium.CircleMarker(
                location=item["govmap_pt"],
                radius=6,
                color=color_hex,
                fill=True,
                fill_color=color_hex,
                fill_opacity=0.85,
                popup=folium.Popup(f"<b>[GovMap]</b><br/>{popup_html}", max_width=300),
                tooltip=f"#{rank} GovMap: {item['address']} ({item['max_dist']:.0f}m)",
                class_name=f"mismatch-item engine-govmap rank-{rank}"
            ).add_to(m)

        # 2. Google Marker (Square)
        if has_google:
            folium.RegularPolygonMarker(
                location=item["google_pt"],
                number_of_sides=4,
                radius=6,
                color=color_hex,
                fill=True,
                fill_color=color_hex,
                fill_opacity=0.85,
                popup=folium.Popup(f"<b>[Google]</b><br/>{popup_html}", max_width=300),
                tooltip=f"#{rank} Google: {item['address']} ({item['max_dist']:.0f}m)",
                class_name=f"mismatch-item engine-google rank-{rank}"
            ).add_to(m)

        # 3. OSM Marker (Triangle)
        if has_osm:
            folium.RegularPolygonMarker(
                location=item["osm_pt"],
                number_of_sides=3,
                radius=6,
                color=color_hex,
                fill=True,
                fill_color=color_hex,
                fill_opacity=0.85,
                popup=folium.Popup(f"<b>[OSM]</b><br/>{popup_html}", max_width=300),
                tooltip=f"#{rank} OSM: {item['address']} ({item['max_dist']:.0f}m)",
                class_name=f"mismatch-item engine-osm rank-{rank}"
            ).add_to(m)

        # 4. Connector lines (Pairwise connecting lines with individual classes)
        # GovMap - Google
        if has_govmap and has_google:
            folium.PolyLine(
                locations=[item["govmap_pt"], item["google_pt"]],
                color=color_hex, weight=2, opacity=0.7,
                class_name=f"mismatch-item line-govmap-google rank-{rank}"
            ).add_to(m)

        # GovMap - OSM
        if has_govmap and has_osm:
            folium.PolyLine(
                locations=[item["govmap_pt"], item["osm_pt"]],
                color=color_hex, weight=2, opacity=0.7,
                class_name=f"mismatch-item line-govmap-osm rank-{rank}"
            ).add_to(m)

        # Google - OSM
        if has_google and has_osm:
            folium.PolyLine(
                locations=[item["google_pt"], item["osm_pt"]],
                color=color_hex, weight=2, opacity=0.7,
                class_name=f"mismatch-item line-google-osm rank-{rank}"
            ).add_to(m)

    # --- INJECT INTERACTIVE CONTROL PANEL & LEGEND ---
    control_panel_html = f"""
    <div id="slider-control-box" style="
        position: fixed; 
        top: 20px; right: 20px; width: 330px;
        background-color: white; z-index: 9999; font-family: Arial, sans-serif;
        font-size: 13px; border: 2px solid #777; padding: 14px; border-radius: 8px;
        box-shadow: 0 4px 10px rgba(0,0,0,0.3); max-height: 90vh; overflow-y: auto;
    ">
        <h4 style="margin: 0 0 6px 0; color: #333;">Spatial Mismatch Filter</h4>
        
        <!-- Shape Legend & Toggles -->
        <div style="background: #f8f9fa; border: 1px solid #ddd; border-radius: 5px; padding: 8px; margin-bottom: 12px;">
            <div style="font-weight: bold; margin-bottom: 6px; font-size: 12px; color: #444;">Engines & Legend:</div>
            
            <label style="display: flex; align-items: center; cursor: pointer; margin-bottom: 4px;">
                <input type="checkbox" id="chkGovmap" checked onchange="updateMapFilter()" style="margin-right: 8px;"/>
                <svg width="14" height="14" style="margin-right: 6px;"><circle cx="7" cy="7" r="5" fill="#555" stroke="#000" stroke-width="1"/></svg>
                GovMap (Circle)
            </label>
            
            <label style="display: flex; align-items: center; cursor: pointer; margin-bottom: 4px;">
                <input type="checkbox" id="chkGoogle" checked onchange="updateMapFilter()" style="margin-right: 8px;"/>
                <svg width="14" height="14" style="margin-right: 6px;"><rect x="2" y="2" width="10" height="10" fill="#555" stroke="#000" stroke-width="1"/></svg>
                Google (Square)
            </label>
            
            <label style="display: flex; align-items: center; cursor: pointer; margin-bottom: 4px;">
                <input type="checkbox" id="chkOsm" checked onchange="updateMapFilter()" style="margin-right: 8px;"/>
                <svg width="14" height="14" style="margin-right: 6px;"><polygon points="7,2 13,12 1,12" fill="#555" stroke="#000" stroke-width="1"/></svg>
                OSM (Triangle)
            </label>

            <hr style="margin: 6px 0; border: 0; border-top: 1px solid #e0e0e0;"/>

            <label style="display: flex; align-items: center; cursor: pointer;">
                <input type="checkbox" id="chkLines" checked onchange="updateMapFilter()" style="margin-right: 8px;"/>
                <svg width="18" height="10" style="margin-right: 6px;"><line x1="0" y1="5" x2="18" y2="5" stroke="#555" stroke-width="2"/></svg>
                Connector Lines
            </label>
        </div>

        <div style="font-size: 11px; color: #666; margin-bottom: 8px; font-style: italic;">
            Logarithmic Distance Controls
        </div>
        
        <!-- Logarithmic Min Slider -->
        <label for="minSlider"><b>Min Distance:</b> 
            <span id="minValLabel" style="color: blue; font-weight: bold;">{min_error_val:.0f}</span> m
        </label>
        <input type="range" id="minSlider" 
               min="{log_min}" max="{log_max}" 
               value="{log_min}" step="0.001" 
               style="width: 100%; margin: 2px 0 10px 0;" oninput="updateMapFilter()">

        <!-- Logarithmic Max Slider -->
        <label for="maxSlider"><b>Max Distance (Cap Outliers):</b> 
            <span id="maxValLabel" style="color: red; font-weight: bold;">{max_error_val:.0f}</span> m
        </label>
        <input type="range" id="maxSlider" 
               min="{log_min}" max="{log_max}" 
               value="{log_max}" step="0.001" 
               style="width: 100%; margin: 2px 0;" oninput="updateMapFilter()">

        <hr style="margin: 10px 0; border: 0; border-top: 1px solid #ccc;"/>
        
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <span><b>Visible Locations:</b></span>
            <span id="visibleCount" style="font-weight: bold; font-size: 14px; color: #0275d8;">{len(top_records)} / {len(top_records)}</span>
        </div>

        <!-- Color Gradient Bar -->
        <div style="margin-top: 10px;">
            <div style="
                background: linear-gradient(to right, red, orange, yellow, cyan, blue);
                height: 12px; border-radius: 3px; border: 1px solid #aaa;
            "></div>
            <div style="display: flex; justify-content: space-between; font-size: 10px; color: #444; margin-top: 2px;">
                <span>#1 Worst (Red)</span>
                <span>#100 Mildest (Blue)</span>
            </div>
        </div>
    </div>

    <script>
    const recordData = {json.dumps(js_records)};

    function setClassDisplay(className, isVisible) {{
        const elements = document.getElementsByClassName(className);
        for (let el of elements) {{
            el.style.display = isVisible ? '' : 'none';
        }}
    }}

    function updateMapFilter() {{
        let logMinVal = parseFloat(document.getElementById('minSlider').value);
        let logMaxVal = parseFloat(document.getElementById('maxSlider').value);

        if (logMinVal > logMaxVal) {{
            logMinVal = logMaxVal;
            document.getElementById('minSlider').value = logMinVal;
        }}

        const minMeters = Math.pow(10, logMinVal);
        const maxMeters = Math.pow(10, logMaxVal);

        document.getElementById('minValLabel').innerText = Math.round(minMeters).toLocaleString();
        document.getElementById('maxValLabel').innerText = Math.round(maxMeters).toLocaleString();

        const showGovmap = document.getElementById('chkGovmap').checked;
        const showGoogle = document.getElementById('chkGoogle').checked;
        const showOsm = document.getElementById('chkOsm').checked;
        const showLines = document.getElementById('chkLines').checked;

        let visibleLocationsCount = 0;

        recordData.forEach(item => {{
            const withinDistance = (item.dist >= minMeters) && (item.dist <= maxMeters);
            if (withinDistance) visibleLocationsCount++;

            // Engine point visibility depends on distance range + engine checkbox
            const govmapVisible = withinDistance && showGovmap && item.hasGovmap;
            const googleVisible = withinDistance && showGoogle && item.hasGoogle;
            const osmVisible = withinDistance && showOsm && item.hasOsm;

            setClassDisplay('engine-govmap rank-' + item.rank, govmapVisible);
            setClassDisplay('engine-google rank-' + item.rank, googleVisible);
            setClassDisplay('engine-osm rank-' + item.rank, osmVisible);

            // A line is shown ONLY IF master line toggle is ON AND BOTH connected engine endpoints are active
            setClassDisplay('line-govmap-google rank-' + item.rank, showLines && govmapVisible && googleVisible);
            setClassDisplay('line-govmap-osm rank-' + item.rank, showLines && govmapVisible && osmVisible);
            setClassDisplay('line-google-osm rank-' + item.rank, showLines && googleVisible && osmVisible);
        }});

        document.getElementById('visibleCount').innerText = visibleLocationsCount + ' / ' + recordData.length;
    }}
    </script>
    """

    m.get_root().html.add_child(folium.Element(control_panel_html))

    # Save output HTML
    m.save(OUTPUT_HTML)
    print(f"\nSuccessfully generated interactive map with dynamic line visibility: {OUTPUT_HTML.resolve()}")


if __name__ == "__main__":
    main()