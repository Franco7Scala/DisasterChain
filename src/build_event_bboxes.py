import argparse
import math
from pathlib import Path
from typing import Iterable, List

import pandas as pd

from support.constants import (
    RECENT_EMDAT_ADMIN_UNIT_BBOXES_CSV,
    RECENT_EMDAT_EVENT_BBOXES_CSV,
)


BBOX_COLUMNS = ["bbox_min_lon", "bbox_min_lat", "bbox_max_lon", "bbox_max_lat"]
EVENT_COLUMNS = [
    "emdat_disaster_id",
    "disaster_type",
    "country",
    "iso",
    "start_date",
    "has_direct_coordinates",
    "location",
]


def unique_join(values: Iterable, separator: str = " | ") -> str:
    seen = []
    for value in values:
        if pd.isna(value):
            continue
        text = str(value).strip()
        if not text or text.lower() == "nan" or text in seen:
            continue
        seen.append(text)
    return separator.join(seen)


def approximate_bbox_metrics(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> tuple:
    center_lat = (min_lat + max_lat) / 2
    width_km = max(0.0, max_lon - min_lon) * 111.32 * math.cos(math.radians(center_lat))
    height_km = max(0.0, max_lat - min_lat) * 111.32
    return width_km, height_km, width_km * height_km


def bbox_quality(
    area_km2: float,
    width_km: float,
    height_km: float,
    matched_units: int,
    review_area_km2: float,
    large_area_km2: float,
    review_side_km: float,
    review_unit_count: int,
) -> tuple:
    reasons = []
    if area_km2 >= large_area_km2:
        reasons.append("very_large_area")
    elif area_km2 >= review_area_km2:
        reasons.append("large_area")
    if width_km >= review_side_km:
        reasons.append("large_width")
    if height_km >= review_side_km:
        reasons.append("large_height")
    if matched_units >= review_unit_count:
        reasons.append("many_admin_units")

    if not reasons:
        return "usable", ""
    if "very_large_area" in reasons:
        return "review_very_large", ",".join(reasons)
    return "review", ",".join(reasons)


def event_base_row(group: pd.DataFrame) -> dict:
    first = group.iloc[0]
    return {
        column: first.get(column, "")
        for column in EVENT_COLUMNS
        if column in group.columns
    }


def matched_event_row(
    group: pd.DataFrame,
    review_area_km2: float,
    large_area_km2: float,
    review_side_km: float,
    review_unit_count: int,
) -> dict:
    matched = group[group["match_status"].eq("matched")].copy()
    base = event_base_row(group)

    bbox_values = matched[BBOX_COLUMNS].apply(pd.to_numeric, errors="coerce")
    min_lon = bbox_values["bbox_min_lon"].min()
    min_lat = bbox_values["bbox_min_lat"].min()
    max_lon = bbox_values["bbox_max_lon"].max()
    max_lat = bbox_values["bbox_max_lat"].max()
    centroid_lon = (min_lon + max_lon) / 2
    centroid_lat = (min_lat + max_lat) / 2

    unit_keys = (
        matched["unit_level"].astype(str)
        + ":"
        + matched["unit_id"].fillna("").astype(str)
        + ":"
        + matched["unit_name"].fillna("").astype(str)
    )
    matched_units = unit_keys.nunique()
    width_km, height_km, area_km2 = approximate_bbox_metrics(min_lon, min_lat, max_lon, max_lat)
    quality, quality_reasons = bbox_quality(
        area_km2=area_km2,
        width_km=width_km,
        height_km=height_km,
        matched_units=matched_units,
        review_area_km2=review_area_km2,
        large_area_km2=large_area_km2,
        review_side_km=review_side_km,
        review_unit_count=review_unit_count,
    )

    base.update(
        {
            "bbox_status": "matched",
            "bbox_source": "gadm_admin_units_union",
            "bbox_min_lon": round(float(min_lon), 6),
            "bbox_min_lat": round(float(min_lat), 6),
            "bbox_max_lon": round(float(max_lon), 6),
            "bbox_max_lat": round(float(max_lat), 6),
            "centroid_lon": round(float(centroid_lon), 6),
            "centroid_lat": round(float(centroid_lat), 6),
            "bbox_width_km_approx": round(float(width_km), 4),
            "bbox_height_km_approx": round(float(height_km), 4),
            "bbox_area_km2_approx": round(float(area_km2), 4),
            "bbox_quality": quality,
            "bbox_quality_reasons": quality_reasons,
            "matched_admin_unit_rows": len(matched),
            "matched_admin_units": matched_units,
            "matched_unit_levels": unique_join(sorted(matched["unit_level"].dropna().unique()), ","),
            "match_methods": unique_join(matched["match_method"].dropna().unique(), ","),
            "geometry_source_files": unique_join(matched["geometry_source_file"].dropna().unique()),
            "bbox_note": "union of matched administrative unit bounding boxes",
        }
    )
    return base


def unmatched_event_row(group: pd.DataFrame) -> dict:
    base = event_base_row(group)
    base.update(
        {
            "bbox_status": "unmatched",
            "bbox_source": "",
            "bbox_min_lon": "",
            "bbox_min_lat": "",
            "bbox_max_lon": "",
            "bbox_max_lat": "",
            "centroid_lon": "",
            "centroid_lat": "",
            "bbox_width_km_approx": "",
            "bbox_height_km_approx": "",
            "bbox_area_km2_approx": "",
            "bbox_quality": "unmatched",
            "bbox_quality_reasons": "",
            "matched_admin_unit_rows": 0,
            "matched_admin_units": 0,
            "matched_unit_levels": "",
            "match_methods": "",
            "geometry_source_files": "",
            "bbox_note": "",
        }
    )
    return base


def build_event_bboxes(
    admin_unit_bboxes: pd.DataFrame,
    only_matched: bool = False,
    review_area_km2: float = 100000.0,
    large_area_km2: float = 1000000.0,
    review_side_km: float = 500.0,
    review_unit_count: int = 20,
) -> pd.DataFrame:
    rows: List[dict] = []
    for _, group in admin_unit_bboxes.groupby("emdat_disaster_id", sort=False):
        has_match = group["match_status"].eq("matched").any()
        if has_match:
            rows.append(
                matched_event_row(
                    group,
                    review_area_km2=review_area_km2,
                    large_area_km2=large_area_km2,
                    review_side_km=review_side_km,
                    review_unit_count=review_unit_count,
                )
            )
        elif not only_matched:
            rows.append(unmatched_event_row(group))
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate administrative-unit bounding boxes into one event-level "
            "bounding box per EM-DAT disaster."
        )
    )
    parser.add_argument(
        "--admin-unit-bboxes-csv",
        default=RECENT_EMDAT_ADMIN_UNIT_BBOXES_CSV,
    )
    parser.add_argument("--output-csv", default=RECENT_EMDAT_EVENT_BBOXES_CSV)
    parser.add_argument(
        "--only-matched",
        action="store_true",
        help="Write only events with at least one matched administrative unit.",
    )
    parser.add_argument(
        "--top-largest",
        type=int,
        default=10,
        help="Number of largest event bounding boxes to print for manual review.",
    )
    parser.add_argument(
        "--review-area-km2",
        type=float,
        default=100000.0,
        help="Mark matched event bboxes at or above this area as review.",
    )
    parser.add_argument(
        "--large-area-km2",
        type=float,
        default=1000000.0,
        help="Mark matched event bboxes at or above this area as review_very_large.",
    )
    parser.add_argument(
        "--review-side-km",
        type=float,
        default=500.0,
        help="Mark matched event bboxes with width/height at or above this value as review.",
    )
    parser.add_argument(
        "--review-unit-count",
        type=int,
        default=20,
        help="Mark event bboxes with at least this many matched admin units as review.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.admin_unit_bboxes_csv)
    output_path = Path(args.output_csv)

    if not input_path.exists():
        raise SystemExit(
            "Admin-unit bbox CSV not found.\n"
            f"Expected path: {input_path}\n\n"
            "Run build_admin_unit_bboxes.py first."
        )

    admin_unit_bboxes = pd.read_csv(input_path)
    missing_columns = [column for column in ["emdat_disaster_id", "match_status", *BBOX_COLUMNS] if column not in admin_unit_bboxes.columns]
    if missing_columns:
        raise SystemExit(f"Missing required column(s): {', '.join(missing_columns)}")

    event_bboxes = build_event_bboxes(
        admin_unit_bboxes,
        only_matched=args.only_matched,
        review_area_km2=args.review_area_km2,
        large_area_km2=args.large_area_km2,
        review_side_km=args.review_side_km,
        review_unit_count=args.review_unit_count,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    event_bboxes.to_csv(output_path, index=False)

    matched = event_bboxes["bbox_status"].eq("matched")
    print(f"Input admin-unit rows: {len(admin_unit_bboxes)}")
    print(f"Input events: {admin_unit_bboxes['emdat_disaster_id'].nunique()}")
    print(f"Output events: {len(event_bboxes)}")
    print(f"Matched event bboxes: {int(matched.sum())}")
    if len(event_bboxes):
        print(f"Matched event percent: {round(matched.mean() * 100, 2)}%")
    if "bbox_quality" in event_bboxes.columns:
        print("BBox quality counts:")
        print(event_bboxes["bbox_quality"].value_counts().to_string())
    print(f"Output CSV: {output_path}")

    largest = event_bboxes.loc[matched].sort_values(
        "bbox_area_km2_approx",
        ascending=False,
    ).head(args.top_largest)
    if len(largest):
        print()
        print("Largest matched event bounding boxes for review:")
        for _, row in largest.iterrows():
            print(
                f"- {row['emdat_disaster_id']} | {row.get('country', '')} | "
                f"{row.get('disaster_type', '')} | "
                f"{row['bbox_area_km2_approx']} km2 | "
                f"{row['matched_admin_units']} unit(s)"
            )


if __name__ == "__main__":
    main()
