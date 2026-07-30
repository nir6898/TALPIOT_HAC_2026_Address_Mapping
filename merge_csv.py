import argparse
import pandas as pd


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Merge two geomapping CSV files with configurable conflict resolution policy."
    )
    parser.add_argument(
        "file1", type=str, help="Path to the first CSV file (File A)"
    )
    parser.add_argument(
        "file2", type=str, help="Path to the second CSV file (File B)"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="merged_output.csv",
        help="Output merged CSV path (default: merged_output.csv)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="Allowed coordinate difference (in ITM units/meters) before flagging a conflict (default: 1.0)",
    )
    parser.add_argument(
        "--prefer",
        type=str,
        choices=["A", "B", "none"],
        default="none",
        help="Policy on coordinate mismatch: 'A' to pick File A, 'B' to pick File B, 'none' to keep both as conflicting (default: none)",
    )
    return parser.parse_args()


def clean_str(val):
    """Normalize string fields for comparison."""
    if pd.isna(val) or val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none") else s


def parse_float(val):
    """Safe float parser."""
    try:
        return float(val) if pd.notna(val) and str(val).strip() != "" else None
    except (ValueError, TypeError):
        return None


def coords_match(row1, row2, tolerance=1.0):
    """Check if ITM coordinates match within a given tolerance."""
    x1, y1 = parse_float(row1.get("itm_x")), parse_float(row1.get("itm_y"))
    x2, y2 = parse_float(row2.get("itm_x")), parse_float(row2.get("itm_y"))

    if x1 is None or y1 is None or x2 is None or y2 is None:
        return False

    return abs(x1 - x2) <= tolerance and abs(y1 - y2) <= tolerance


def resolve_coordinate_conflict(row1, row2, prefer_policy):
    """Handles cases where coordinates differ based on user policy."""
    r1 = row1.to_dict()
    r2 = row2.to_dict()

    if prefer_policy == "A":
        r1["status"] = "resolved_preference"
        r1["source_file"] = "File A (Preferred)"
        return [r1]

    elif prefer_policy == "B":
        r2["status"] = "resolved_preference"
        r2["source_file"] = "File B (Preferred)"
        return [r2]

    else:
        # Default: Keep both and flag mismatch
        r1["status"] = "conflict_coordinate_mismatch"
        r2["status"] = "conflict_coordinate_mismatch"
        return [r1, r2]


def merge_datasets(file1_path, file2_path, output_path, tolerance=1.0, prefer_policy="none"):
    df1 = pd.read_csv(file1_path, dtype=str).fillna("")
    df2 = pd.read_csv(file2_path, dtype=str).fillna("")

    # Assign initial metadata
    df1["source_file"] = "File A"
    df2["source_file"] = "File B"

    df1["status"] = "ok"
    df2["status"] = "ok"

    df1.columns = df1.columns.str.strip()
    df2.columns = df2.columns.str.strip()

    # Generate matching key (City|Street|HouseNumber)
    def make_key(row):
        city = clean_str(row.get("city"))
        street = clean_str(row.get("street"))
        num = clean_str(row.get("house_number"))
        return f"{city}|{street}|{num}"

    df1["_match_key"] = df1.apply(make_key, axis=1)
    df2["_match_key"] = df2.apply(make_key, axis=1)

    merged_results = []

    # Map file2 keys for quick lookup
    dict2 = {}
    for _, row in df2.iterrows():
        key = row["_match_key"]
        dict2.setdefault(key, []).append(row)

    processed_keys_file2 = set()

    for _, row1 in df1.iterrows():
        key = row1["_match_key"]

        if key in dict2:
            processed_keys_file2.add(key)
            matches_in_2 = dict2[key]

            for row2 in matches_in_2:
                type1 = clean_str(row1.get("match_type")).lower()
                type2 = clean_str(row2.get("match_type")).lower()

                # Rule 1: One is 'exact' and the other is not -> Prefer 'exact'
                if type1 == "exact" and type2 != "exact":
                    merged_results.append(row1.to_dict())
                elif type2 == "exact" and type1 != "exact":
                    merged_results.append(row2.to_dict())

                # Rule 2: Both are 'exact' OR both are non-exact -> Compare coordinates
                else:
                    if coords_match(row1, row2, tolerance=tolerance):
                        # Coordinates match -> single output row
                        res = row1.to_dict()
                        res["source_file"] = "Both (File A & B)"
                        merged_results.append(res)
                    else:
                        # Coordinate Mismatch -> Apply `--prefer` policy
                        resolved = resolve_coordinate_conflict(row1, row2, prefer_policy)
                        merged_results.extend(resolved)
        else:
            # Address exists only in File 1
            merged_results.append(row1.to_dict())

    # Add remaining entries from File 2 that had no match in File 1
    for key, rows in dict2.items():
        if key not in processed_keys_file2:
            for row2 in rows:
                merged_results.append(row2.to_dict())

    res_df = pd.DataFrame(merged_results)
    if "_match_key" in res_df.columns:
        res_df.drop(columns=["_match_key"], inplace=True)

    # Clean layout order
    base_cols = [c for c in res_df.columns if c not in ["source_file", "status"]]
    final_cols = base_cols + ["source_file", "status"]
    res_df = res_df[final_cols]

    res_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"Merge complete! Output written to '{output_path}'.")
    print(f"Total output records: {len(res_df)}")


if __name__ == "__main__":
    args = parse_arguments()
    merge_datasets(
        file1_path=args.file1,
        file2_path=args.file2,
        output_path=args.output,
        tolerance=args.tolerance,
        prefer_policy=args.prefer.upper() if args.prefer != "none" else "none",
    )