import argparse
import os
import pandas as pd


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Convert geomapping Excel file to target CSV format with smart street filtering."
    )
    parser.add_argument(
        "input_excel", type=str, help="Path to input .xlsx file"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="output.csv",
        help="Output CSV file path",
    )
    parser.add_argument(
        "--city", type=str, default=None, help="Filter by city name"
    )
    parser.add_argument(
        "--street", type=str, default=None, help="Filter by street name"
    )
    return parser.parse_args()


def format_house_number(val):
    if pd.isna(val) or val == "" or val == 0:
        return ""
    try:
        num_float = float(val)
        return (
            str(int(num_float))
            if num_float.is_integer()
            else str(num_float)
        )
    except (ValueError, TypeError):
        return str(val).strip()


def convert_geo_data(
    input_filepath, output_filepath, city_filter=None, street_filter=None
):
    if not os.path.exists(input_filepath):
        print(f"Error: File not found at '{input_filepath}'")
        return

    df = pd.read_excel(input_filepath)
    df.columns = df.columns.str.strip()

    # Apply command-line filters if provided
    if city_filter:
        df = df[df["city"].astype(str).str.strip() == city_filter.strip()]
    if street_filter:
        df = df[df["street"].astype(str).str.strip() == street_filter.strip()]

    if df.empty:
        print("No matching records found after filtering.")
        return

    # Clean city/street strings and extract clean house numbers
    df["city_clean"] = df["city"].astype(str).str.strip().fillna("")
    df["street_clean"] = df["street"].astype(str).str.strip().fillna("")
    df["house_number_clean"] = df["number"].apply(format_house_number)

    # STEP 1: Identify streets that contain at least one specific house number
    streets_with_house_numbers = set(
        df[df["house_number_clean"] != ""][
            ["city_clean", "street_clean"]
        ].itertuples(index=False, name=None)
    )

    # STEP 2: Filter out less-specific entries (missing house number) if a specific entry exists
    def should_keep(row):
        street_key = (row["city_clean"], row["street_clean"])
        has_no_number = row["house_number_clean"] == ""

        # If this row has no house number, but other rows on the street DO have numbers -> drop it
        if has_no_number and street_key in streets_with_house_numbers:
            return False
        return True

    initial_count = len(df)
    df = df[df.apply(should_keep, axis=1)].reset_index(drop=True)
    dropped_count = initial_count - len(df)

    if dropped_count > 0:
        print(
            f"Ignored {dropped_count} generic street entry/entries in favor of specific house numbers."
        )

    # STEP 3: Analyze coordinate uniqueness for remaining rows
    street_coord_counts = {}
    for (city_val, street_val), group in df.groupby(
        ["city_clean", "street_clean"]
    ):
        valid_coords = group[(group["X"] > 0) & (group["Y"] > 0)]
        unique_coords = set(zip(valid_coords["X"], valid_coords["Y"]))
        street_coord_counts[(city_val, street_val)] = len(unique_coords)

    # STEP 4: Build output rows
    output_rows = []

    for idx, row in df.iterrows():
        city = row["city_clean"]
        street = row["street_clean"]
        house_number = row["house_number_clean"]

        if house_number:
            address = f"{street} {house_number}, {city}"
        else:
            address = f"{street}, {city}"

        x_val = row.get("X", 0)
        y_val = row.get("Y", 0)

        try:
            itm_x = float(x_val) if pd.notna(x_val) else 0.0
            itm_y = float(y_val) if pd.notna(y_val) else 0.0
        except (ValueError, TypeError):
            itm_x, itm_y = 0.0, 0.0

        # Determine match_type
        if itm_x <= 0 or itm_y <= 0:
            match_type = "none"
        else:
            num_unique_coords = street_coord_counts.get((city, street), 0)
            if num_unique_coords <= 1 or not house_number:
                match_type = "street"
            else:
                match_type = "exact"

        row_id = f"001027-073567-{idx:05d}-X"

        output_rows.append(
            {
                "id": row_id,
                "address": address,
                "city": city,
                "street": street,
                "house_number": house_number,
                "entrance": "",
                "zip": "",
                "match_type": match_type,
                "itm_x": f"{itm_x:.2f}" if itm_x > 0 else "",
                "itm_y": f"{itm_y:.2f}" if itm_y > 0 else "",
                "utm_x": "",
                "utm_y": "",
            }
        )

    out_df = pd.DataFrame(output_rows)
    out_df.to_csv(output_filepath, index=False, encoding="utf-8-sig")
    print(
        f"Successfully converted {len(out_df)} records to '{output_filepath}'"
    )


if __name__ == "__main__":
    args = parse_arguments()
    convert_geo_data(
        input_filepath=args.input_excel,
        output_filepath=args.output,
        city_filter=args.city,
        street_filter=args.street,
    )