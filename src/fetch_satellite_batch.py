import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from support.constants import (
    CLEANED_DISASTERS_OUTPUT_PATH,
    SATELLITE_BATCH_DEFAULT_LIMIT,
    SATELLITE_BATCH_DEFAULT_MIN_START_DATE,
    SATELLITE_BATCH_DEFAULT_SLEEP_SECONDS,
    SATELLITE_BATCH_SUMMARY_CSV,
    SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    SATELLITE_DEFAULT_IMAGE_SIZE,
    SATELLITE_DEFAULT_MAX_CLOUD_COVER,
    SATELLITE_DEFAULT_S2_CLOUD_CANDIDATE_LIMIT,
    SATELLITE_DEFAULT_S2_CLOUD_EVAL_SIZE,
    SATELLITE_DEFAULT_S2_USABLE_LOCAL_CLOUD_COVER,
    SATELLITE_DEFAULT_S2_WATER_THRESHOLD,
    SATELLITE_DEFAULT_WINDOW_DAYS,
)
from support.satellite_engine import (
    SATELLITE_OUTPUT_DIR,
    SatelliteEvent,
    SatelliteRunConfig,
    run_satellite_event,
)


SUMMARY_FIELDS = [
    "run_started_at",
    "event_id",
    "country",
    "location",
    "start_date",
    "latitude",
    "longitude",
    "status",
    "manifest_path",
    "recommended_primary_layer",
    "s1_change_detection_available",
    "s2_pre_usable",
    "s2_post_usable",
    "s2_change_detection_usable",
    "s2_water_change_mask_available",
    "s2_pre_local_cloud_cover",
    "s2_post_local_cloud_cover",
    "s1_candidate_area_km2",
    "s2_pre_candidate_water_area_km2",
    "s2_post_candidate_water_area_km2",
    "s2_candidate_new_water_area_km2",
    "output_count",
    "elapsed_seconds",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Sentinel-1/Sentinel-2 extraction workflow for multiple "
            "events and write a compact batch summary CSV."
        )
    )
    parser.add_argument("--events-csv", default=CLEANED_DISASTERS_OUTPUT_PATH)
    parser.add_argument(
        "--event-id",
        action="append",
        dest="event_ids",
        help=(
            "Specific EM-DAT event id to process. Can be repeated or contain "
            "comma-separated ids. If omitted, events are selected from filters."
        ),
    )
    parser.add_argument("--disaster-type", default="Flood")
    parser.add_argument("--country", help="Optional exact country filter")
    parser.add_argument(
        "--min-start-date",
        default=SATELLITE_BATCH_DEFAULT_MIN_START_DATE,
        help="Earliest event start date to include, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=SATELLITE_BATCH_DEFAULT_LIMIT,
        help="Maximum number of selected events. Use 0 to process all matches.",
    )
    parser.add_argument("--output-dir", default=SATELLITE_OUTPUT_DIR)
    parser.add_argument("--summary-csv", default=SATELLITE_BATCH_SUMMARY_CSV)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run events even when their manifest.json already exists.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop the batch at the first failed event.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=SATELLITE_BATCH_DEFAULT_SLEEP_SECONDS,
        help="Pause between real API runs to be gentle with the service.",
    )
    parser.add_argument(
        "--aoi-half-size-km",
        type=float,
        default=SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    )
    parser.add_argument("--window-days", type=int, default=SATELLITE_DEFAULT_WINDOW_DAYS)
    parser.add_argument("--image-size", type=int, default=SATELLITE_DEFAULT_IMAGE_SIZE)
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=SATELLITE_DEFAULT_MAX_CLOUD_COVER,
    )
    parser.add_argument(
        "--s2-cloud-eval-size",
        type=int,
        default=SATELLITE_DEFAULT_S2_CLOUD_EVAL_SIZE,
    )
    parser.add_argument(
        "--s2-cloud-candidate-limit",
        type=int,
        default=SATELLITE_DEFAULT_S2_CLOUD_CANDIDATE_LIMIT,
    )
    parser.add_argument(
        "--s2-usable-local-cloud-cover",
        type=float,
        default=SATELLITE_DEFAULT_S2_USABLE_LOCAL_CLOUD_COVER,
    )
    parser.add_argument(
        "--s2-water-threshold",
        type=float,
        default=SATELLITE_DEFAULT_S2_WATER_THRESHOLD,
    )
    return parser.parse_args()


def load_event_rows(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def expand_event_ids(values: Optional[Iterable[str]]) -> List[str]:
    if not values:
        return []

    event_ids = []
    for value in values:
        event_ids.extend(part.strip() for part in value.split(",") if part.strip())
    return event_ids


def parse_iso_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def has_required_event_fields(row: Dict[str, str]) -> bool:
    return all(
        row.get(field)
        for field in ["emdat_disaster_id", "latitude", "longitude", "start_date"]
    )


def row_to_event(row: Dict[str, str]) -> SatelliteEvent:
    if not has_required_event_fields(row):
        raise ValueError("missing event id, coordinates, or start date")

    return SatelliteEvent(
        event_id=row["emdat_disaster_id"],
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        start_date=row["start_date"],
        country=row.get("country") or None,
        disaster_type=row.get("disaster_type") or None,
        location=row.get("location") or row.get("emdat_location") or None,
    )


def select_events(args: argparse.Namespace) -> List[SatelliteEvent]:
    rows = load_event_rows(args.events_csv)
    requested_ids = expand_event_ids(args.event_ids)

    if requested_ids:
        rows_by_id = {row.get("emdat_disaster_id"): row for row in rows}
        missing = [event_id for event_id in requested_ids if event_id not in rows_by_id]
        if missing:
            raise SystemExit(
                "Event id not found in CSV: " + ", ".join(sorted(missing))
            )
        selected_rows = [rows_by_id[event_id] for event_id in requested_ids]
    else:
        min_start_date = parse_iso_date(args.min_start_date)
        selected_rows = []
        for row in rows:
            if not has_required_event_fields(row):
                continue
            if args.disaster_type and row.get("disaster_type") != args.disaster_type:
                continue
            if args.country and row.get("country") != args.country:
                continue
            try:
                start_date = parse_iso_date(row["start_date"])
            except ValueError:
                continue
            if start_date < min_start_date:
                continue
            selected_rows.append(row)

    if args.limit > 0:
        selected_rows = selected_rows[: args.limit]

    events = []
    for row in selected_rows:
        try:
            events.append(row_to_event(row))
        except ValueError as exc:
            event_id = row.get("emdat_disaster_id", "<missing id>")
            print(f"Skipping {event_id}: {exc}")

    if not events:
        raise SystemExit("No valid events selected.")
    return events


def build_config(args: argparse.Namespace) -> SatelliteRunConfig:
    return SatelliteRunConfig(
        aoi_half_size_km=args.aoi_half_size_km,
        window_days=args.window_days,
        image_size=args.image_size,
        max_cloud_cover=args.max_cloud_cover,
        s2_cloud_eval_size=args.s2_cloud_eval_size,
        s2_cloud_candidate_limit=args.s2_cloud_candidate_limit,
        s2_usable_local_cloud_cover=args.s2_usable_local_cloud_cover,
        s2_water_threshold=args.s2_water_threshold,
    )


def nested_get(data: Dict, path: Iterable[str], default: str = ""):
    current = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def load_manifest(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_path_for(output_dir: str, event: SatelliteEvent) -> Path:
    return Path(output_dir) / event.event_id / "manifest.json"


def summary_row(
    run_started_at: str,
    event: SatelliteEvent,
    status: str,
    elapsed_seconds: float,
    manifest: Optional[Dict] = None,
    manifest_path: Optional[Path] = None,
    error: str = "",
) -> Dict[str, object]:
    manifest = manifest or {}
    quality = manifest.get("quality", {})
    scenes = manifest.get("scenes", {})
    statistics = manifest.get("statistics", {})

    if manifest_path is None and manifest.get("manifest_path"):
        manifest_path = Path(manifest["manifest_path"])

    return {
        "run_started_at": run_started_at,
        "event_id": event.event_id,
        "country": event.country or "",
        "location": event.location or "",
        "start_date": event.start_date,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "status": status,
        "manifest_path": str(manifest_path or ""),
        "recommended_primary_layer": quality.get("recommended_primary_layer", ""),
        "s1_change_detection_available": quality.get(
            "s1_change_detection_available", ""
        ),
        "s2_pre_usable": quality.get("s2_pre_usable", ""),
        "s2_post_usable": quality.get("s2_post_usable", ""),
        "s2_change_detection_usable": quality.get("s2_change_detection_usable", ""),
        "s2_water_change_mask_available": quality.get(
            "s2_water_change_mask_available", ""
        ),
        "s2_pre_local_cloud_cover": nested_get(
            scenes,
            ["s2_pre", "local_cloud_cover"],
        ),
        "s2_post_local_cloud_cover": nested_get(
            scenes,
            ["s2_post", "local_cloud_cover"],
        ),
        "s1_candidate_area_km2": nested_get(
            statistics,
            [
                "s1_change_mask_png",
                "classes",
                "candidate_radar_change",
                "area_km2",
            ],
        ),
        "s2_pre_candidate_water_area_km2": nested_get(
            statistics,
            [
                "s2_pre_mndwi_mask_png",
                "classes",
                "candidate_water",
                "area_km2",
            ],
        ),
        "s2_post_candidate_water_area_km2": nested_get(
            statistics,
            [
                "s2_post_mndwi_mask_png",
                "classes",
                "candidate_water",
                "area_km2",
            ],
        ),
        "s2_candidate_new_water_area_km2": nested_get(
            statistics,
            [
                "s2_water_change_mask_png",
                "classes",
                "candidate_new_water",
                "area_km2",
            ],
        ),
        "output_count": len(manifest.get("outputs", {})),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "error": error,
    }


def summary_header_matches(path: Path) -> bool:
    if not path.exists():
        return True
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
    return header == SUMMARY_FIELDS


def append_summary(summary_path: str, row: Dict[str, object]) -> None:
    path = Path(summary_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not summary_header_matches(path):
        archived_path = path.with_name(
            f"{path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}"
        )
        path.replace(archived_path)
        print(f"Archived old summary with outdated columns: {archived_path}")

    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def print_dry_run(events: List[SatelliteEvent], args: argparse.Namespace) -> None:
    print(f"Dry run selected {len(events)} event(s). No API calls will be made.")
    print(
        "Filters: "
        f"disaster_type={args.disaster_type!r}, "
        f"country={args.country!r}, "
        f"min_start_date={args.min_start_date!r}, "
        f"limit={args.limit}"
    )
    for index, event in enumerate(events, start=1):
        print(
            f"{index}. {event.event_id} | {event.country or '-'} | "
            f"{event.start_date} | lat={event.latitude:.5f}, "
            f"lon={event.longitude:.5f} | {event.location or '-'}"
        )


def run_batch(args: argparse.Namespace) -> None:
    events = select_events(args)
    if args.dry_run:
        print_dry_run(events, args)
        return

    config = build_config(args)
    run_started_at = datetime.now().isoformat(timespec="seconds")
    print(f"Selected {len(events)} event(s). Summary: {args.summary_csv}")

    for index, event in enumerate(events, start=1):
        start = time.perf_counter()
        manifest_path = manifest_path_for(args.output_dir, event)
        print(f"[{index}/{len(events)}] {event.event_id}: starting")

        try:
            if manifest_path.exists() and not args.force:
                manifest = load_manifest(manifest_path)
                status = "skipped_existing"
                print(f"[{index}/{len(events)}] {event.event_id}: manifest exists, skipped")
            else:
                manifest = run_satellite_event(event, config, output_dir=args.output_dir)
                manifest_path = Path(manifest["manifest_path"])
                status = "completed"
                recommended = nested_get(
                    manifest,
                    ["quality", "recommended_primary_layer"],
                    default="unknown",
                )
                print(
                    f"[{index}/{len(events)}] {event.event_id}: completed "
                    f"(recommended={recommended})"
                )

            elapsed = time.perf_counter() - start
            append_summary(
                args.summary_csv,
                summary_row(
                    run_started_at,
                    event,
                    status,
                    elapsed,
                    manifest=manifest,
                    manifest_path=manifest_path,
                ),
            )
        except Exception as exc:
            elapsed = time.perf_counter() - start
            error = str(exc)
            print(f"[{index}/{len(events)}] {event.event_id}: error: {error}")
            append_summary(
                args.summary_csv,
                summary_row(
                    run_started_at,
                    event,
                    "error",
                    elapsed,
                    manifest_path=manifest_path,
                    error=error,
                ),
            )
            if args.stop_on_error:
                raise SystemExit(error) from exc

        if index < len(events) and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    print("Batch completed.")
    print(f"Summary CSV: {args.summary_csv}")


def main() -> None:
    run_batch(parse_args())


if __name__ == "__main__":
    main()
