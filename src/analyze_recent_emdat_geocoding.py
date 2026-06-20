import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from support.constants import (
    EMDAT_INPUT_PATH,
    RECENT_EMDAT_ADMIN_UNITS_CSV,
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


def load_json_list(value) -> List[Dict]:
    if not has_text(value):
        return []
    try:
        parsed = json.loads(str(value).strip())
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def unit_from_gadm_item(item: Dict) -> Dict:
    for level in (3, 2, 1):
        gid = item.get(f"gid_{level}")
        name = item.get(f"name_{level}")
        if gid or name:
            return {
                "unit_level": level,
                "unit_id": gid or "",
                "unit_name": name or "",
                "migration_method": item.get("migration_method") or "",
            }
    return {
        "unit_level": "",
        "unit_id": "",
        "unit_name": "",
        "migration_method": item.get("migration_method") or "",
    }


def unit_from_admin_item(item: Dict) -> Dict:
    for level in (3, 2, 1):
        code = item.get(f"adm{level}_code")
        name = item.get(f"adm{level}_name")
        if code or name:
            return {
                "unit_level": level,
                "unit_id": code or "",
                "unit_name": name or "",
                "migration_method": "",
            }
    return {"unit_level": "", "unit_id": "", "unit_name": "", "migration_method": ""}


def admin_unit_rows(row: pd.Series) -> List[Dict]:
    start_date = build_start_date(row)
    base = {
        "emdat_disaster_id": row["DisNo."],
        "disaster_type": row.get("Disaster Type"),
        "country": row.get("Country"),
        "iso": row.get("ISO"),
        "start_date": start_date.strftime("%Y-%m-%d") if start_date is not None else "",
        "has_direct_coordinates": pd.notna(row["Latitude"]) and pd.notna(row["Longitude"]),
        "location": row.get("Location") if has_text(row.get("Location")) else "",
    }

    rows = []
    seen = set()
    for source_field, parser, source_priority in [
        ("GADM Admin Units", unit_from_gadm_item, 1),
        ("Admin Units", unit_from_admin_item, 2),
    ]:
        for item in load_json_list(row.get(source_field)):
            unit = parser(item)
            key = (
                source_field,
                str(unit.get("unit_level")),
                str(unit.get("unit_id")),
                str(unit.get("unit_name")),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    **base,
                    "source_field": source_field,
                    "source_priority": source_priority,
                    **unit,
                }
            )
    return rows


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
    parser.add_argument(
        "--admin-units-csv",
        default=RECENT_EMDAT_ADMIN_UNITS_CSV,
    )
    parser.add_argument("--limit", type=int, help="Optional row limit for quick checks.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    emdat_path = Path(args.emdat_path)
    if not emdat_path.exists():
        raise SystemExit(
            "EM-DAT input file not found.\n"
            f"Expected path: {emdat_path}\n\n"
            "Raw data files are not tracked by Git. Copy the EM-DAT Excel file "
            "to the cluster data/ folder or pass its path with --emdat-path."
        )

    emdat = pd.read_excel(emdat_path)

    rows = []
    unit_rows = []
    cutoff = pd.Timestamp(args.start_date)
    for _, row in emdat.iterrows():
        start_date = build_start_date(row)
        if start_date is None or start_date < cutoff:
            continue
        rows.append(candidate_row(row))
        unit_rows.extend(admin_unit_rows(row))
        if args.limit and len(rows) >= args.limit:
            break

    candidates = pd.DataFrame(rows)
    summary = pd.DataFrame(summary_rows(candidates, args.start_date))
    admin_units = pd.DataFrame(
        unit_rows,
        columns=[
            "emdat_disaster_id",
            "disaster_type",
            "country",
            "iso",
            "start_date",
            "has_direct_coordinates",
            "location",
            "source_field",
            "source_priority",
            "unit_level",
            "unit_id",
            "unit_name",
            "migration_method",
        ],
    )

    candidates_path = Path(args.candidates_csv)
    summary_path = Path(args.summary_csv)
    admin_units_path = Path(args.admin_units_csv)
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(candidates_path, index=False)
    summary.to_csv(summary_path, index=False)
    admin_units.to_csv(admin_units_path, index=False)

    print(f"Analyzed EM-DAT events from {args.start_date}: {len(candidates)}")
    print(f"Candidates CSV: {candidates_path}")
    print(f"Summary CSV: {summary_path}")
    print(f"Admin units CSV: {admin_units_path}")
    print()
    for _, row in summary.iterrows():
        print(f"{row['metric']}: {row['count']} ({row['percent']}%)")
    print(
        "admin_unit_rows: "
        f"{len(admin_units)} "
        f"({admin_units['emdat_disaster_id'].nunique() if len(admin_units) else 0} events)"
    )


if __name__ == "__main__":
    main()
