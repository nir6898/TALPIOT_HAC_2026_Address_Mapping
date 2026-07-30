import csv
import re

INPUT_CSV = "google_maps_zipcode_compare\merged__odata__dimona_geocoded_20260730.csv"
OUTPUT_CSV = "cleaned_output.csv"

def extract_house_number(val):
    if not val:
        return ""
    val_str = val.strip()
    # Extracts number with optional Hebrew/English suffix or slash (e.g. 26, 26א, 26/2)
    match = re.search(r'(\d+[\s\-/]?[a-zA-Z\u0590-\u05FF]?)', val_str)
    return match.group(1).strip() if match else val_str

def rebuild_address(street, house_num, city):
    """Rebuilds full address cleanly: e.g. 'אבן גבירול 26, דימונה'"""
    parts = []
    if street:
        parts.append(street.strip())
    if house_num:
        parts.append(house_num.strip())
    
    combined_street_num = " ".join(parts) if parts else ""
    
    if city and city.strip():
        return f"{combined_street_num}, {city.strip()}" if combined_street_num else city.strip()
    return combined_street_num

with open(INPUT_CSV, mode="r", encoding="utf-8-sig") as infile, \
     open(OUTPUT_CSV, mode="w", encoding="utf-8-sig", newline="") as outfile:
    
    reader = csv.DictReader(infile)
    writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
    
    writer.writeheader()
    for row in reader:
        # 1. Clean the house number
        clean_num = extract_house_number(row.get("house_number", ""))
        row["house_number"] = clean_num
        
        # 2. Rebuild the address field cleanly
        street = row.get("street", "")
        city = row.get("city", "")
        row["address"] = rebuild_address(street, clean_num, city)
        
        writer.writerow(row)

print(f"Cleaned file saved to {OUTPUT_CSV}")