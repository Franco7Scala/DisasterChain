import argparse
from pathlib import Path
from typing import Dict, List, Set

import pandas as pd

from support.constants import (
    RECENT_EMDAT_ADM2_AOIS_CSV,
    RECENT_EMDAT_GEOCODING_CANDIDATES_CSV,
    RECENT_EMDAT_GEOCODING_COVERAGE_SUMMARY_CSV,
    RECENT_EMDAT_REMAINING_EVENTS_CSV,
)


EVENT_ID_COLUMN = "emdat_disaster_id"


def true_values(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def event_ids(frame: pd.DataFrame, mask: pd.Series) -> Set[str]:
    return set(frame.loc[mask, EVENT_ID_COLUMN].dropna().astype(str))


def coverage_rows(
    candidates: pd.DataFrame,
    aois: pd.DataFrame,
) -> tuple[List[Dict], pd.DataFrame]:
    all_events = set(candidates[EVENT_ID_COLUMN].dropna().astype(str))
    direct = event_ids(candidates, true_values(candidates["has_direct_coordinates"]))
    adm2 = event_ids(aois, aois["bbox_status"].eq("matched"))
    overlap = direct & adm2
    covered = direct | adm2
    remaining = all_events - covered
    remaining_rows = candidates[
        candidates[EVENT_ID_COLUMN].astype(str).isin(remaining)
    ].copy()

    total = len(all_events)

    def metric(name: str, count: int) -> Dict:
        return {
            "metric": name,
            "count": count,
            "percent_of_total": round(count / total * 100, 2) if total else 0.0,
        }

    rows = [
        metric("total_events", total),
        metric("direct_coordinates", len(direct)),
        metric("matched_adm2", len(adm2)),
        metric("direct_and_adm2_overlap", len(overlap)),
        metric("new_events_from_adm2", len(adm2 - direct)),
        metric("currently_geolocated", len(covered)),
        metric("remaining_without_coordinates", len(remaining)),
    ]

    status_counts = remaining_rows["geocoding_status"].value_counts()
    for status in ["gadm_or_admin_units", "location_text_only", "missing_location"]:
        rows.append(metric(f"remaining_{status}", int(status_counts.get(status, 0))))

    return rows, remaining_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize the union of direct EM-DAT coordinates and matched ADM2 "
            "AOIs, then save events that still require geocoding."
        )
    )
    parser.add_argument("--candidates-csv", default=RECENT_EMDAT_GEOCODING_CANDIDATES_CSV)
    parser.add_argument("--adm2-aois-csv", default=RECENT_EMDAT_ADM2_AOIS_CSV)
    parser.add_argument(
        "--summary-csv",
        default=RECENT_EMDAT_GEOCODING_COVERAGE_SUMMARY_CSV,
    )
    parser.add_argument(
        "--remaining-events-csv",
        default=RECENT_EMDAT_REMAINING_EVENTS_CSV,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidates_path = Path(args.candidates_csv)
    aois_path = Path(args.adm2_aois_csv)

    for path, label in [
        (candidates_path, "Candidates CSV"),
        (aois_path, "ADM2 AOIs CSV"),
    ]:
        if not path.exists():
            raise SystemExit(f"{label} not found: {path}")

    candidates = pd.read_csv(candidates_path)
    aois = pd.read_csv(aois_path)
    required_candidates = {
        EVENT_ID_COLUMN,
        "has_direct_coordinates",
        "geocoding_status",
    }
    required_aois = {EVENT_ID_COLUMN, "bbox_status"}
    missing_candidates = required_candidates - set(candidates.columns)
    missing_aois = required_aois - set(aois.columns)
    if missing_candidates:
        raise SystemExit(
            "Candidates CSV missing column(s): "
            + ", ".join(sorted(missing_candidates))
        )
    if missing_aois:
        raise SystemExit(
            "ADM2 AOIs CSV missing column(s): "
            + ", ".join(sorted(missing_aois))
        )

    rows, remaining = coverage_rows(candidates, aois)
    summary = pd.DataFrame(rows)
    summary_path = Path(args.summary_csv)
    remaining_path = Path(args.remaining_events_csv)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    remaining_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(summary_path, index=False)
    remaining.to_csv(remaining_path, index=False)

    print("Geocoding coverage summary:")
    for row in rows:
        print(
            f"{row['metric']}: {row['count']} "
            f"({row['percent_of_total']}%)"
        )
    print()
    print(f"Summary CSV: {summary_path}")
    print(f"Remaining events CSV: {remaining_path}")


if __name__ == "__main__":
    main()
