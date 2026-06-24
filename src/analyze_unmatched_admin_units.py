import argparse
from pathlib import Path
from typing import Dict, Iterable, List, Set

import pandas as pd

from support.constants import (
    GADM_DATA_DIR,
    GADM_FILE_PREFIX,
    RECENT_EMDAT_ADMIN_UNIT_BBOXES_CSV,
    RECENT_EMDAT_ADMIN_UNITS_CSV,
    RECENT_EMDAT_REMAINING_EVENTS_CSV,
    RECENT_EMDAT_UNMATCHED_ADMIN_DIAGNOSTICS_CSV,
    RECENT_EMDAT_UNMATCHED_ADMIN_SUMMARY_CSV,
)


EVENT_ID_COLUMN = "emdat_disaster_id"
TARGET_LEVEL = 2


def normalize_level(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return -1


def unique_join(values: Iterable) -> str:
    result = []
    for value in values:
        if pd.isna(value):
            continue
        text = str(value).strip()
        if not text or text.lower() == "nan" or text in result:
            continue
        result.append(text)
    return " | ".join(result)


def available_adm2_isos(gadm_dir: Path) -> Set[str]:
    result = set()
    patterns = [
        f"{GADM_FILE_PREFIX}_???_{TARGET_LEVEL}.json",
        f"{GADM_FILE_PREFIX}_???_{TARGET_LEVEL}.json.zip",
        f"{GADM_FILE_PREFIX}_???_{TARGET_LEVEL}.geojson",
        f"{GADM_FILE_PREFIX}_???_{TARGET_LEVEL}.geojson.zip",
    ]
    for pattern in patterns:
        for path in gadm_dir.glob(pattern):
            parts = path.name.split("_")
            if len(parts) >= 3:
                result.add(parts[1].upper())
    return result


def diagnose_event(
    event: pd.Series,
    admin_rows: pd.DataFrame,
    bbox_rows: pd.DataFrame,
    local_adm2_isos: Set[str],
) -> Dict:
    event_id = str(event[EVENT_ID_COLUMN])
    event_admin = admin_rows[
        admin_rows[EVENT_ID_COLUMN].astype(str).eq(event_id)
    ].copy()
    event_bbox = bbox_rows[
        bbox_rows[EVENT_ID_COLUMN].astype(str).eq(event_id)
    ].copy()

    event_admin["_level"] = event_admin["unit_level"].apply(normalize_level)
    event_bbox["_level"] = event_bbox["unit_level"].apply(normalize_level)
    adm2_admin = event_admin[event_admin["_level"].eq(TARGET_LEVEL)]
    adm2_bbox = event_bbox[event_bbox["_level"].eq(TARGET_LEVEL)]

    iso = str(event.get("iso") or "").strip().upper()
    has_local_file = iso in local_adm2_isos
    matched_adm2_rows = int(adm2_bbox["match_status"].eq("matched").sum())
    gadm_id_rows = adm2_admin[
        adm2_admin["source_field"].eq("GADM Admin Units")
        & adm2_admin["unit_id"].notna()
    ]

    if adm2_admin.empty:
        category = "no_adm2_source_rows"
    elif not has_local_file:
        category = "missing_gadm_adm2_file"
    elif matched_adm2_rows:
        category = "matched_adm2_not_in_remaining_set"
    elif len(gadm_id_rows):
        category = "unmatched_gadm_ids"
    else:
        category = "unmatched_adm2_names"

    return {
        EVENT_ID_COLUMN: event_id,
        "disaster_type": event.get("disaster_type", ""),
        "country": event.get("country", ""),
        "iso": iso,
        "start_date": event.get("start_date", ""),
        "location": event.get("location", ""),
        "diagnostic_category": category,
        "available_source_levels": unique_join(
            sorted(level for level in event_admin["_level"].unique() if level >= 0)
        ),
        "adm2_source_rows": len(adm2_admin),
        "adm2_gadm_id_rows": len(gadm_id_rows),
        "adm2_name_only_rows": len(adm2_admin) - len(gadm_id_rows),
        "matched_adm2_rows": matched_adm2_rows,
        "gadm_adm2_file_available": has_local_file,
        "adm2_unit_ids": unique_join(adm2_admin["unit_id"]),
        "adm2_unit_names": unique_join(adm2_admin["unit_name"]),
    }


def build_summary(diagnostics: pd.DataFrame) -> pd.DataFrame:
    total = len(diagnostics)
    counts = diagnostics["diagnostic_category"].value_counts()
    rows: List[Dict] = [
        {
            "metric": "remaining_admin_events",
            "count": total,
            "percent": 100.0 if total else 0.0,
        }
    ]
    for category, count in counts.items():
        rows.append(
            {
                "metric": category,
                "count": int(count),
                "percent": round(count / total * 100, 2) if total else 0.0,
            }
        )
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Classify why remaining EM-DAT administrative-unit events do not "
            "yet have a matched GADM ADM2 area."
        )
    )
    parser.add_argument("--remaining-events-csv", default=RECENT_EMDAT_REMAINING_EVENTS_CSV)
    parser.add_argument("--admin-units-csv", default=RECENT_EMDAT_ADMIN_UNITS_CSV)
    parser.add_argument(
        "--admin-unit-bboxes-csv",
        default=RECENT_EMDAT_ADMIN_UNIT_BBOXES_CSV,
    )
    parser.add_argument("--gadm-dir", default=GADM_DATA_DIR)
    parser.add_argument(
        "--diagnostics-csv",
        default=RECENT_EMDAT_UNMATCHED_ADMIN_DIAGNOSTICS_CSV,
    )
    parser.add_argument(
        "--summary-csv",
        default=RECENT_EMDAT_UNMATCHED_ADMIN_SUMMARY_CSV,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_paths = [
        (Path(args.remaining_events_csv), "Remaining events CSV"),
        (Path(args.admin_units_csv), "Admin units CSV"),
        (Path(args.admin_unit_bboxes_csv), "Admin-unit bboxes CSV"),
    ]
    for path, label in input_paths:
        if not path.exists():
            raise SystemExit(f"{label} not found: {path}")

    remaining = pd.read_csv(args.remaining_events_csv)
    admin_units = pd.read_csv(args.admin_units_csv)
    admin_bboxes = pd.read_csv(args.admin_unit_bboxes_csv)
    admin_remaining = remaining[
        remaining["geocoding_status"].eq("gadm_or_admin_units")
    ].copy()

    local_isos = available_adm2_isos(Path(args.gadm_dir))
    rows = [
        diagnose_event(event, admin_units, admin_bboxes, local_isos)
        for _, event in admin_remaining.iterrows()
    ]
    diagnostics = pd.DataFrame(rows)
    summary = build_summary(diagnostics)

    diagnostics_path = Path(args.diagnostics_csv)
    summary_path = Path(args.summary_csv)
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics.to_csv(diagnostics_path, index=False)
    summary.to_csv(summary_path, index=False)

    print("Unmatched administrative-unit diagnostics:")
    for _, row in summary.iterrows():
        print(f"{row['metric']}: {row['count']} ({row['percent']}%)")
    print()
    print(f"Countries with local ADM2 files: {len(local_isos)}")
    print(f"Diagnostics CSV: {diagnostics_path}")
    print(f"Summary CSV: {summary_path}")


if __name__ == "__main__":
    main()
