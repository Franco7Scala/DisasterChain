import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from support.constants import SATELLITE_BATCH_SUMMARY_CSV, SATELLITE_RANKED_EVENTS_CSV


RANK_FIELDS = [
    "rank",
    "rank_group",
    "rank_score",
    "rank_reason",
    "event_id",
    "country",
    "location",
    "start_date",
    "status",
    "recommended_primary_layer",
    "s1_change_detection_available",
    "s2_change_detection_usable",
    "s2_water_change_mask_available",
    "s2_candidate_new_water_area_km2",
    "s2_isolated_new_water_area_km2",
    "s2_new_water_near_persistent_percent",
    "s2_change_unreliable_percent",
    "max_s2_local_cloud_cover",
    "s2_pre_local_cloud_cover",
    "s2_post_local_cloud_cover",
    "s1_candidate_area_km2",
    "s2_pre_candidate_water_area_km2",
    "s2_post_candidate_water_area_km2",
    "manifest_path",
    "error",
]


def display_value(value) -> str:
    return "-" if value == "" or value is None else str(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rank satellite batch results to identify the most useful flood "
            "events for visual inspection and dataset construction."
        )
    )
    parser.add_argument("--summary-csv", default=SATELLITE_BATCH_SUMMARY_CSV)
    parser.add_argument("--output-csv", default=SATELLITE_RANKED_EVENTS_CSV)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--no-mask-context",
        action="store_true",
        help="Do not inspect manifest/mask files while ranking.",
    )
    parser.add_argument(
        "--require-s2-change",
        action="store_true",
        help="Only rank events with a usable Sentinel-2 water-change mask.",
    )
    return parser.parse_args()


def read_rows(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def dedupe_latest(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    latest_by_event = {}
    for row in rows:
        event_id = row.get("event_id", "").strip()
        if event_id:
            latest_by_event[event_id] = row
    return list(latest_by_event.values())


def as_bool(row: Dict[str, str], key: str) -> bool:
    return row.get(key, "").strip().lower() == "true"


def as_float(row: Dict[str, str], key: str) -> Optional[float]:
    value = row.get(key, "").strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def max_cloud(row: Dict[str, str]) -> Optional[float]:
    values = [
        value
        for value in [
            as_float(row, "s2_pre_local_cloud_cover"),
            as_float(row, "s2_post_local_cloud_cover"),
        ]
        if value is not None
    ]
    return max(values) if values else None


def _dilate(mask, radius: int = 3):
    import numpy as np

    padded = np.pad(mask, radius, mode="constant", constant_values=False)
    output = np.zeros_like(mask, dtype=bool)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                output |= padded[
                    radius + dy : radius + dy + mask.shape[0],
                    radius + dx : radius + dx + mask.shape[1],
                ]
    return output


def _water_change_mask_context(mask_path: Path, new_water_area_km2: float) -> Dict:
    import numpy as np
    from PIL import Image

    pixels = np.asarray(Image.open(mask_path).convert("RGB"))
    red = pixels[:, :, 0]
    green = pixels[:, :, 1]
    blue = pixels[:, :, 2]

    new_water = (blue > 200) & (red < 30) & (green < 140)
    persistent_water = (
        (red < 20)
        & (green > 90)
        & (green < 150)
        & (blue > 130)
        & (blue < 200)
    )

    new_pixels = int(new_water.sum())
    if new_pixels == 0:
        return {
            "s2_new_water_near_persistent_percent": 0.0,
            "s2_isolated_new_water_area_km2": 0.0,
        }

    near_persistent = new_water & _dilate(persistent_water, radius=3)
    near_percent = round((int(near_persistent.sum()) / new_pixels) * 100, 4)
    isolated_area = round(new_water_area_km2 * (1 - near_percent / 100), 4)
    return {
        "s2_new_water_near_persistent_percent": near_percent,
        "s2_isolated_new_water_area_km2": isolated_area,
    }


def load_mask_context(row: Dict[str, str]) -> Dict:
    manifest_path = Path(row.get("manifest_path", ""))
    if not manifest_path.exists():
        return {}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    stats = manifest.get("statistics", {}).get("s2_water_change_mask_png", {})
    classes = stats.get("classes", {})
    cloud_percent = as_float_from_value(classes.get("cloud_or_shadow", {}).get("percent"))
    nodata_percent = as_float_from_value(classes.get("nodata", {}).get("percent"))
    new_water_area = as_float(row, "s2_candidate_new_water_area_km2")

    context = {}
    if cloud_percent is not None or nodata_percent is not None:
        context["s2_change_unreliable_percent"] = round(
            (cloud_percent or 0.0) + (nodata_percent or 0.0),
            4,
        )

    output_path = Path(manifest.get("outputs", {}).get("s2_water_change_mask_png", ""))
    if not output_path.exists():
        local_output_path = manifest_path.parent / "s2_water_change_mask.png"
        if local_output_path.exists():
            output_path = local_output_path

    if output_path.exists() and new_water_area is not None:
        try:
            context.update(_water_change_mask_context(output_path, new_water_area))
        except (OSError, ImportError, ValueError):
            pass
    return context


def as_float_from_value(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rank_group(row: Dict[str, str]) -> Tuple[str, int, str]:
    if row.get("status") == "error":
        return "error", 0, "batch row has an error"

    s1_available = as_bool(row, "s1_change_detection_available")
    s2_change = as_bool(row, "s2_change_detection_usable")
    s2_mask = as_bool(row, "s2_water_change_mask_available")
    new_water_area = as_float(row, "s2_candidate_new_water_area_km2") or 0.0

    if s1_available and s2_change and s2_mask and new_water_area > 0:
        return "best_s1_s2_change", 100, "S1 and S2 change layers available"
    if s1_available and s2_change and s2_mask:
        return "s1_s2_change_no_new_water", 90, "S1 and S2 usable, but no new water"
    if s2_change and s2_mask and new_water_area > 0:
        return "s2_change", 80, "S2 water-change layer available"
    if s2_change and s2_mask:
        return "s2_change_no_new_water", 70, "S2 usable, but no new water"
    if s1_available:
        return "s1_primary", 50, "S1 change layer available"
    if as_bool(row, "s2_post_usable"):
        return "s2_post_only", 40, "only S2 post-event looks usable"
    return "manual_review", 10, "missing reliable automatic change layer"


def rank_score(row: Dict[str, str]) -> Tuple[str, float, str]:
    group, base_score, reason = rank_group(row)
    new_water_area = as_float(row, "s2_candidate_new_water_area_km2") or 0.0
    isolated_new_water_area = (
        as_float_from_value(row.get("s2_isolated_new_water_area_km2"))
        or new_water_area
    )
    s1_area = as_float(row, "s1_candidate_area_km2") or 0.0
    cloud = max_cloud(row)
    unreliable_percent = as_float_from_value(row.get("s2_change_unreliable_percent"))
    near_persistent_percent = as_float_from_value(
        row.get("s2_new_water_near_persistent_percent")
    )

    score = float(base_score)
    score += min(isolated_new_water_area * 20.0, 20.0)
    score += min(s1_area * 0.1, 5.0)
    if cloud is not None:
        score += max(0.0, 30.0 - cloud) / 3.0
    if unreliable_percent is not None:
        score -= min(unreliable_percent / 2.0, 15.0)
    if near_persistent_percent is not None:
        score -= min(near_persistent_percent / 12.5, 8.0)

    return group, round(score, 4), reason


def ranked_rows(
    rows: List[Dict[str, str]],
    require_s2_change: bool,
    use_mask_context: bool,
) -> List[Dict[str, str]]:
    ranked = []
    for row in rows:
        row = dict(row)
        if use_mask_context:
            row.update(load_mask_context(row))
        if require_s2_change and not as_bool(row, "s2_water_change_mask_available"):
            continue
        group, score, reason = rank_score(row)
        output = {
            "rank": 0,
            "rank_group": group,
            "rank_score": score,
            "rank_reason": reason,
            "event_id": row.get("event_id", ""),
            "country": row.get("country", ""),
            "location": row.get("location", ""),
            "start_date": row.get("start_date", ""),
            "status": row.get("status", ""),
            "recommended_primary_layer": row.get("recommended_primary_layer", ""),
            "s1_change_detection_available": row.get(
                "s1_change_detection_available", ""
            ),
            "s2_change_detection_usable": row.get("s2_change_detection_usable", ""),
            "s2_water_change_mask_available": row.get(
                "s2_water_change_mask_available", ""
            ),
            "s2_candidate_new_water_area_km2": row.get(
                "s2_candidate_new_water_area_km2", ""
            ),
            "s2_isolated_new_water_area_km2": row.get(
                "s2_isolated_new_water_area_km2", ""
            ),
            "s2_new_water_near_persistent_percent": row.get(
                "s2_new_water_near_persistent_percent", ""
            ),
            "s2_change_unreliable_percent": row.get(
                "s2_change_unreliable_percent", ""
            ),
            "max_s2_local_cloud_cover": "" if max_cloud(row) is None else max_cloud(row),
            "s2_pre_local_cloud_cover": row.get("s2_pre_local_cloud_cover", ""),
            "s2_post_local_cloud_cover": row.get("s2_post_local_cloud_cover", ""),
            "s1_candidate_area_km2": row.get("s1_candidate_area_km2", ""),
            "s2_pre_candidate_water_area_km2": row.get(
                "s2_pre_candidate_water_area_km2", ""
            ),
            "s2_post_candidate_water_area_km2": row.get(
                "s2_post_candidate_water_area_km2", ""
            ),
            "manifest_path": row.get("manifest_path", ""),
            "error": row.get("error", ""),
        }
        ranked.append(output)

    ranked.sort(
        key=lambda row: (
            float(row["rank_score"]),
            as_float(row, "s2_candidate_new_water_area_km2") or 0.0,
        ),
        reverse=True,
    )
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    return ranked


def write_ranked(path: str, rows: List[Dict[str, str]]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RANK_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(all_rows: List[Dict[str, str]], ranked: List[Dict[str, str]], top: int) -> None:
    group_counts = Counter(row["rank_group"] for row in ranked)
    status_counts = Counter(row.get("status", "") for row in all_rows)

    print(f"Unique events ranked: {len(ranked)}")
    print("Status counts: " + ", ".join(f"{key}={value}" for key, value in status_counts.items()))
    print("Rank groups: " + ", ".join(f"{key}={value}" for key, value in group_counts.items()))
    print()
    print("Top events:")
    for row in ranked[:top]:
        area = display_value(row["s2_candidate_new_water_area_km2"])
        isolated_area = display_value(row["s2_isolated_new_water_area_km2"])
        cloud = display_value(row["max_s2_local_cloud_cover"])
        print(
            f"{row['rank']:>2}. {row['event_id']} | {row['country']} | "
            f"{row['rank_group']} | score={row['rank_score']} | "
            f"new_water_km2={area} | isolated_km2={isolated_area} | "
            f"max_s2_cloud={cloud}"
        )


def main() -> None:
    args = parse_args()
    raw_rows = read_rows(args.summary_csv)
    latest_rows = dedupe_latest(raw_rows)
    ranked = ranked_rows(
        latest_rows,
        args.require_s2_change,
        use_mask_context=not args.no_mask_context,
    )
    write_ranked(args.output_csv, ranked)
    print_summary(latest_rows, ranked, args.top)
    print(f"Ranked CSV: {args.output_csv}")


if __name__ == "__main__":
    main()
