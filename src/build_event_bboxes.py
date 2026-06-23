import argparse
import math
import re
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
AOI_ID_CLEANUP_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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


def normalize_unit_level(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return -1


def deduplicate_matched_aois(matched: pd.DataFrame) -> pd.DataFrame:
    if matched.empty:
        return matched

    working = matched.copy()
    for column in BBOX_COLUMNS:
        working[f"_{column}_key"] = pd.to_numeric(working[column], errors="coerce").round(6)

    working["_unit_level_key"] = working["unit_level"].apply(normalize_unit_level)
    working["_source_rank"] = working["source_field"].map(
        {
            "GADM Admin Units": 0,
            "Admin Units": 1,
        }
    ).fillna(2)
    working["_match_method_rank"] = working["match_method"].map(
        {
            "gadm_id": 0,
            "name_level": 1,
        }
    ).fillna(2)

    dedupe_columns = [
        "emdat_disaster_id",
        "_unit_level_key",
        "_bbox_min_lon_key",
        "_bbox_min_lat_key",
        "_bbox_max_lon_key",
        "_bbox_max_lat_key",
    ]
    working = working.sort_values(["_source_rank", "_match_method_rank"], kind="stable")
    return working.drop_duplicates(dedupe_columns, keep="first").copy()


def safe_aoi_part(value) -> str:
    text = str(value).strip() if value is not None and not pd.isna(value) else ""
    text = AOI_ID_CLEANUP_RE.sub("_", text)
    return text.strip("_") or "unknown"


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
            "aoi_id": "",
            "aoi_index": "",
            "admin_unit_level": "",
            "admin_unit_id": "",
            "admin_unit_name": "",
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


def matched_single_unit_row(
    group: pd.DataFrame,
    aoi_index: int,
    review_area_km2: float,
    large_area_km2: float,
    review_side_km: float,
) -> dict:
    first = group.iloc[0]
    base = event_base_row(group)

    bbox_values = group[BBOX_COLUMNS].apply(pd.to_numeric, errors="coerce")
    min_lon = bbox_values["bbox_min_lon"].min()
    min_lat = bbox_values["bbox_min_lat"].min()
    max_lon = bbox_values["bbox_max_lon"].max()
    max_lat = bbox_values["bbox_max_lat"].max()
    centroid_lon = (min_lon + max_lon) / 2
    centroid_lat = (min_lat + max_lat) / 2
    width_km, height_km, area_km2 = approximate_bbox_metrics(min_lon, min_lat, max_lon, max_lat)
    quality, quality_reasons = bbox_quality(
        area_km2=area_km2,
        width_km=width_km,
        height_km=height_km,
        matched_units=1,
        review_area_km2=review_area_km2,
        large_area_km2=large_area_km2,
        review_side_km=review_side_km,
        review_unit_count=999999,
    )

    unit_level = normalize_unit_level(first.get("unit_level"))
    unit_id = "" if pd.isna(first.get("unit_id")) else str(first.get("unit_id")).strip()
    unit_name = "" if pd.isna(first.get("unit_name")) else str(first.get("unit_name")).strip()
    unit_token = unit_id or unit_name or f"aoi_{aoi_index}"
    event_id = str(first.get("emdat_disaster_id") or "").strip()

    base.update(
        {
            "aoi_id": f"{safe_aoi_part(event_id)}__adm{unit_level}_{safe_aoi_part(unit_token)}",
            "aoi_index": aoi_index,
            "admin_unit_level": unit_level,
            "admin_unit_id": unit_id,
            "admin_unit_name": unit_name,
            "bbox_status": "matched",
            "bbox_source": "gadm_admin_unit",
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
            "matched_admin_unit_rows": len(group),
            "matched_admin_units": 1,
            "matched_unit_levels": str(unit_level),
            "match_methods": unique_join(group["match_method"].dropna().unique(), ","),
            "geometry_source_files": unique_join(group["geometry_source_file"].dropna().unique()),
            "bbox_note": "single matched administrative unit bounding box",
        }
    )
    return base


def unmatched_event_row(group: pd.DataFrame) -> dict:
    base = event_base_row(group)
    base.update(
        {
            "aoi_id": "",
            "aoi_index": "",
            "admin_unit_level": "",
            "admin_unit_id": "",
            "admin_unit_name": "",
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


def matched_rows_for_level(group: pd.DataFrame, unit_level: int) -> pd.DataFrame:
    matched = group[group["match_status"].eq("matched")].copy()
    if unit_level:
        matched = matched[matched["unit_level"].apply(normalize_unit_level).eq(unit_level)].copy()
    return matched


def build_event_bboxes(
    admin_unit_bboxes: pd.DataFrame,
    only_matched: bool = False,
    unit_level: int = 0,
    review_area_km2: float = 100000.0,
    large_area_km2: float = 1000000.0,
    review_side_km: float = 500.0,
    review_unit_count: int = 20,
) -> pd.DataFrame:
    rows: List[dict] = []
    for _, group in admin_unit_bboxes.groupby("emdat_disaster_id", sort=False):
        working_group = group
        if unit_level:
            level_matches = (
                group["match_status"].eq("matched")
                & group["unit_level"].apply(normalize_unit_level).eq(unit_level)
            )
            if level_matches.any():
                working_group = group[group["match_status"].ne("matched") | level_matches].copy()
            else:
                working_group = group[group["match_status"].ne("matched")].copy()

        has_match = working_group["match_status"].eq("matched").any()
        if has_match:
            rows.append(
                matched_event_row(
                    working_group,
                    review_area_km2=review_area_km2,
                    large_area_km2=large_area_km2,
                    review_side_km=review_side_km,
                    review_unit_count=review_unit_count,
                )
            )
        elif not only_matched:
            output = unmatched_event_row(group)
            if unit_level:
                output["bbox_note"] = f"no matched administrative units at level {unit_level}"
            rows.append(output)
    return pd.DataFrame(rows)


def build_event_unit_aois(
    admin_unit_bboxes: pd.DataFrame,
    only_matched: bool = False,
    unit_level: int = 2,
    review_area_km2: float = 100000.0,
    large_area_km2: float = 1000000.0,
    review_side_km: float = 500.0,
) -> pd.DataFrame:
    rows: List[dict] = []
    for _, event_group in admin_unit_bboxes.groupby("emdat_disaster_id", sort=False):
        matched = deduplicate_matched_aois(matched_rows_for_level(event_group, unit_level=unit_level))
        if matched.empty:
            if not only_matched:
                output = unmatched_event_row(event_group)
                output["bbox_note"] = f"no matched administrative units at level {unit_level}"
                rows.append(output)
            continue

        matched = matched.copy()
        matched["_unit_level_norm"] = matched["unit_level"].apply(normalize_unit_level)
        matched["_unit_id_norm"] = matched["unit_id"].fillna("").astype(str)
        matched["_unit_name_norm"] = matched["unit_name"].fillna("").astype(str)
        unit_columns = ["_unit_level_norm", "_unit_id_norm", "_unit_name_norm"]
        for aoi_index, (_, unit_group) in enumerate(matched.groupby(unit_columns, sort=False), start=1):
            rows.append(
                matched_single_unit_row(
                    unit_group,
                    aoi_index=aoi_index,
                    review_area_km2=review_area_km2,
                    large_area_km2=large_area_km2,
                    review_side_km=review_side_km,
                )
            )
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build event-level or AOI-level bounding boxes from matched "
            "administrative-unit geometries."
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
        "--unit-level",
        type=int,
        default=0,
        help=(
            "Use only matched administrative units from this GADM level. "
            "Use 0 to aggregate all matched levels."
        ),
    )
    parser.add_argument(
        "--separate-units",
        action="store_true",
        help=(
            "Write one AOI row per matched administrative unit instead of "
            "one union bbox per event. If --unit-level is omitted, level 2 is used."
        ),
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

    selected_unit_level = args.unit_level
    if args.separate_units and not selected_unit_level:
        selected_unit_level = 2

    output_rows = (
        build_event_unit_aois(
            admin_unit_bboxes,
            only_matched=args.only_matched,
            unit_level=selected_unit_level,
            review_area_km2=args.review_area_km2,
            large_area_km2=args.large_area_km2,
            review_side_km=args.review_side_km,
        )
        if args.separate_units
        else build_event_bboxes(
            admin_unit_bboxes,
            only_matched=args.only_matched,
            unit_level=selected_unit_level,
            review_area_km2=args.review_area_km2,
            large_area_km2=args.large_area_km2,
            review_side_km=args.review_side_km,
            review_unit_count=args.review_unit_count,
        )
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_rows.to_csv(output_path, index=False)

    matched = output_rows["bbox_status"].eq("matched")
    print(f"Input admin-unit rows: {len(admin_unit_bboxes)}")
    print(f"Input events: {admin_unit_bboxes['emdat_disaster_id'].nunique()}")
    if selected_unit_level:
        print(f"Matched unit level filter: {selected_unit_level}")
    print(f"Output mode: {'separate_units' if args.separate_units else 'event_union'}")
    print(f"Output rows: {len(output_rows)}")
    print(f"Output events: {output_rows['emdat_disaster_id'].nunique() if len(output_rows) else 0}")
    print(f"Matched bbox rows: {int(matched.sum())}")
    if len(output_rows):
        print(f"Matched row percent: {round(matched.mean() * 100, 2)}%")
    if "bbox_quality" in output_rows.columns:
        print("BBox quality counts:")
        print(output_rows["bbox_quality"].value_counts().to_string())
    print(f"Output CSV: {output_path}")

    largest = output_rows.loc[matched].sort_values(
        "bbox_area_km2_approx",
        ascending=False,
    ).head(args.top_largest)
    if len(largest):
        print()
        print("Largest matched bounding boxes for review:")
        for _, row in largest.iterrows():
            print(
                f"- {row['emdat_disaster_id']} | {row.get('country', '')} | "
                f"{row.get('disaster_type', '')} | "
                f"{row['bbox_area_km2_approx']} km2 | "
                f"{row['matched_admin_units']} unit(s)"
            )


if __name__ == "__main__":
    main()
