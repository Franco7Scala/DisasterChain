import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from support.constants import (
    EMDAT_INPUT_PATH,
    RECENT_EMDAT_GEOCODING_CANDIDATES_CSV,
    RECENT_EMDAT_GEOCODING_SUMMARY_CSV,
)


def has_text(value) -> bool:
    if pd.isna(value):
        return False
    text = str(value).strip()
    return bool(text and text.lower() not in {"nan", "none", "null", "[]"})


def compact_json_text(value) -> str:
    if not has_text(value):
        return ""
    text = str(value).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(parsed, list):
        return text
    names = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        for key in ("name_3", "name_2", "name_1", "adm3_name", "adm2_name", "adm1_name"):
            name = item.get(key)
            if name and name not in names:
                names.append(str(name))
                break
    return " | ".join(names) if names else text


def build_start_date(row: pd.Series) -> Optional[pd.Timestamp]:
    if pd.isna(row["Start Year"]) or pd.isna(row["Start Month"]) or pd.isna(row["Start Day"]):
        return None
    return pd.Timestamp(
        year=int(row["Start Year"]),
        month=int(row["Start Month"]),
        day=int(row["Start Day"]),
    )


def geocoding_status(row: pd.Series) -> str:
    has_latlon = pd.notna(row["Latitude"]) and pd.notna(row["Longitude"])
    has_gadm = has_text(row.get("GADM Admin Units"))
    has_admin = has_text(row.get("Admin Units"))
    has_location = has_text(row.get("Location"))

    if has_latlon:
        return "direct_coordinates"
    if has_gadm or has_admin:
        return "gadm_or_admin_units"
    if has_location:
        return "location_text_only"
    return "missing_location"


def candidate_row(row: pd.Series) -> Dict:
    start_date = build_start_date(row)
    status = geocoding_status(row)
    return {
        "emdat_disaster_id": row["DisNo."],
        "disaster_type": row.get("Disaster Type"),
        "disaster_subtype": row.get("Disaster Subtype"),
        "country": row.get("Country"),
        "iso": row.get("ISO"),
        "region": row.get("Region"),
        "start_date": start_date.strftime("%Y-%m-%d") if start_date is not None else "",
        "has_complete_start_date": start_date is not None,
        "geocoding_status": status,
        "has_direct_coordinates": status == "direct_coordinates",
        "has_gadm_or_admin_units": status == "gadm_or_admin_units",
        "has_location_text": has_text(row.get("Location")),
        "latitude": row.get("Latitude") if pd.notna(row.get("Latitude")) else "",
        "longitude": row.get("Longitude") if pd.notna(row.get("Longitude")) else "",
        "location": row.get("Location") if has_text(row.get("Location")) else "",
        "admin_units_text": compact_json_text(row.get("Admin Units")),
        "gadm_admin_units_text": compact_json_text(row.get("GADM Admin Units")),
        "event_name": row.get("Event Name") if has_text(row.get("Event Name")) else "",
    }


def summary_rows(candidates: pd.DataFrame, start_date: str) -> List[Dict]:
    total = len(candidates)
    rows = []

    def add(metric: str, count: int) -> None:
        rows.append(
            {
                "start_date_cutoff": start_date,
                "metric": metric,
                "count": count,
                "percent": round((count / total) * 100, 2) if total else 0.0,
            }
        )

    add("total_events", total)
    add("complete_start_date", int(candidates["has_complete_start_date"].sum()))
    for status, group in candidates.groupby("geocoding_status"):
        add(status, len(group))
    add(
        "recoverable_without_free_text_geocoding",
        int(
            candidates["geocoding_status"].isin(
                ["direct_coordinates", "gadm_or_admin_units"]
            ).sum()
        ),
    )
    add(
        "recoverable_with_location_text",
        int(candidates["geocoding_status"].ne("missing_location").sum()),
    )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze recent EM-DAT events to estimate how many can be "
            "geolocated without relying only on the old GDIS file."
        )
    )
    parser.add_argument("--emdat-path", default=EMDAT_INPUT_PATH)
    parser.add_argument(
        "--start-date",
        default="2014-04-03",
        help="Earliest event start date to include, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--candidates-csv",
        default=RECENT_EMDAT_GEOCODING_CANDIDATES_CSV,
    )
    parser.add_argument(
        "--summary-csv",
        default=RECENT_EMDAT_GEOCODING_SUMMARY_CSV,
    )
    parser.add_argument("--limit", type=int, help="Optional row limit for quick checks.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    emdat = pd.read_excel(args.emdat_path)

    rows = []
    cutoff = pd.Timestamp(args.start_date)
    for _, row in emdat.iterrows():
        start_date = build_start_date(row)
        if start_date is None or start_date < cutoff:
            continue
        rows.append(candidate_row(row))
        if args.limit and len(rows) >= args.limit:
            break

    candidates = pd.DataFrame(rows)
    summary = pd.DataFrame(summary_rows(candidates, args.start_date))

    candidates_path = Path(args.candidates_csv)
    summary_path = Path(args.summary_csv)
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(candidates_path, index=False)
    summary.to_csv(summary_path, index=False)

    print(f"Analyzed EM-DAT events from {args.start_date}: {len(candidates)}")
    print(f"Candidates CSV: {candidates_path}")
    print(f"Summary CSV: {summary_path}")
    print()
    for _, row in summary.iterrows():
        print(f"{row['metric']}: {row['count']} ({row['percent']}%)")


if __name__ == "__main__":
    main()
