import pandas as pd
import requests
import time
import os
from tqdm import tqdm

# --- CONFIGURATION ---
INPUT_FILE = "C:/Users/nir vegh/Downloads/veneto_addresses.csv"
OUTPUT_FILE = "geocoded_veneto_addresses.csv"

# INPUT_FILE = "C:/Users/nir vegh/Downloads/veneto_cities.csv"
# OUTPUT_FILE = "geocoded_veneto_cities.csv"

USER_AGENT = "Veneto_Research_NirV_2026"  # Unique ID for OSM
CHUNK_SIZE = 200  # Number of rows to handle before clearing RAM


def geocode_public(address):
    """Fetches one address. Obeying the 1 req/sec rule."""
    if not address or pd.isna(address):
        return None, None

    url = "https://nominatim.openstreetmap.org/search"
    params = {'q': address, 'format': 'json', 'limit': 1, 'countrycodes': 'it'}
    headers = {'User-Agent': USER_AGENT}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)

        # Handle Rate Limiting (429)
        if r.status_code == 429:
            time.sleep(30)
            return geocode_public(address)

        # OSM strictly requires 1 second between requests
        time.sleep(1.01)

        data = r.json()
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception:
        time.sleep(2)
    return None, None


# 1. Row Counting (so tqdm knows the total scale)
print("Analyzing file size...")
num_lines = sum(1 for _ in open(INPUT_FILE, 'r', encoding='utf-8')) - 1

# 2. Setup Output File (Create header if new)
if not os.path.exists(OUTPUT_FILE):
    # Read just the header of the input file
    header_df = pd.read_csv(INPUT_FILE, nrows=0)
    header_df['latitude'] = None
    header_df['longitude'] = None
    header_df.to_csv(OUTPUT_FILE, index=False)

# 3. Determine how many rows are already done (Resume capability)
already_processed = sum(1 for _ in open(OUTPUT_FILE, 'r', encoding='utf-8')) - 1
print(f"Skipping {already_processed} already geocoded rows.")

# 4. The Main Loop (Using Chunking)
# We use skiprows to start exactly where we left off
reader = pd.read_csv(INPUT_FILE, chunksize=CHUNK_SIZE, skiprows=range(1, already_processed + 1))

with tqdm(total=num_lines, initial=already_processed, desc="Progress") as pbar:
    for chunk in reader:
        lats = []
        lons = []

        # We process the chunk row-by-row (NO threading allowed here!)
        # for addr in chunk['city_of_residance']:
        for addr in chunk['full_address']:
            lat, lon = geocode_public(addr)
            lats.append(lat)
            lons.append(lon)
            pbar.update(1)  # Update bar for every single address

        # Add results to the current chunk
        chunk['latitude'] = lats
        chunk['longitude'] = lons

        # Append the 1,000 processed rows to the CSV
        chunk.to_csv(OUTPUT_FILE, mode='a', header=False, index=False)

        # At the end of this loop, 'chunk' is deleted from memory automatically

print(f"\n✅ All done! Data saved to {OUTPUT_FILE}")