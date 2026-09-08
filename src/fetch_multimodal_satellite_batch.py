import argparse
import csv
import json
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from support.constants import (
    MULTIMODAL_BATCH_DEFAULT_LIMIT,
    MULTIMODAL_BATCH_SUMMARY_CSV,
    MULTIMODAL_DEFAULT_MAX_CLOUD_COVER,
    MULTIMODAL_DEFAULT_OUTPUT_RESOLUTION_M,
    MULTIMODAL_DEFAULT_S3_RESOLUTION_M,
    MULTIMODAL_SATELLITE_OUTPUT_DIR,
    SATELLITE_BATCH_DEFAULT_SLEEP_SECONDS,
    SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    SATELLITE_DEFAULT_WINDOW_DAYS,
)
from support.multimodal_satellite_engine import (
    MultimodalSatelliteConfig,
    run_multimodal_satellite_event,
    manifest_complete,
    manifest_matches,
    SatelliteConfigurationError,
)
from support.satellite_engine import SatelliteEvent, SentinelHubRequestError, require_copernicus_credentials


SUMMARY_FIELDS = [
    "run_started_at",
    "event_id",
    "country",
    "disaster_type",
    "location",
    "start_date",
    "latitude",
    "longitude",
    "status",
    "manifest_path",
    "total_days",
    "sentinel_2_available_days",
    "sentinel_1_available_days",
    "sentinel_3_slstr_available_days",
    "sentinel_2_missing_days",
    "sentinel_1_missing_days",
    "sentinel_3_slstr_missing_days",
    "land_cover_available",
    "land_cover_year",
    "land_cover_product",
    "land_cover_status",
    "sentinel_2_error_days",
    "sentinel_1_error_days",
    "sentinel_3_slstr_error_days",
    "manifest_status",
    "has_any_satellite_data",
    "output_file_count",
    "elapsed_seconds",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the general multimodal satellite extraction workflow for a "
            "batch of disaster events of any type."
        )
    )
    parser.add_argument("--events-csv", default=str(Path(MULTIMODAL_SATELLITE_OUTPUT_DIR) / "events.csv"))
    parser.add_argument(
        "--event-id",
        action="append",
        dest="event_ids",
        help=(
            "Specific EM-DAT event id to process. Can be repeated or contain "
            "comma-separated ids. If omitted, events are selected from filters."
        ),
    )
    parser.add_argument(
        "--disaster-type",
        help="Optional exact disaster type filter, e.g. Flood or Wildfire.",
    )
    parser.add_argument("--country", help="Optional exact country filter")
    parser.add_argument(
        "--min-start-date",
        default="2014-04-03",
        help="Earliest event start date to include, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum number of selected events. Use 0 to process all matches. "
            "Defaults to a small limit when events are selected by filters; "
            "manual --event-id lists are not limited unless this is set."
        ),
    )
    parser.add_argument("--output-dir", default=MULTIMODAL_SATELLITE_OUTPUT_DIR)
    parser.add_argument("--summary-csv", default=MULTIMODAL_BATCH_SUMMARY_CSV)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-free-disk-gb", type=float, default=5.0,
                        help="Stop before the next event if free disk space falls below this reserve")
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
    parser.add_argument(
        "--output-resolution-m",
        type=int,
        default=MULTIMODAL_DEFAULT_OUTPUT_RESOLUTION_M,
    )
    parser.add_argument(
        "--s3-resolution-m",
        type=int,
        default=MULTIMODAL_DEFAULT_S3_RESOLUTION_M,
    )
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=MULTIMODAL_DEFAULT_MAX_CLOUD_COVER,
    )
    parser.add_argument(
        "--no-png-previews",
        action="store_true",
        help="Skip human-inspection PNG previews.",
    )
    parser.add_argument(
        "--skip-land-cover",
        action="store_true",
        help="Skip the one-off WorldCover/land-cover layer.",
    )
    parser.add_argument(
        "--skip-sentinel-3",
        action="store_true",
        help="Skip Sentinel-3 SLSTR thermal layers.",
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

    event = SatelliteEvent(
        event_id=row["emdat_disaster_id"],
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        start_date=row["start_date"],
        country=row.get("country") or None,
        disaster_type=row.get("disaster_type") or None,
        location=row.get("location") or row.get("emdat_location") or None,
    )
    parse_iso_date(event.start_date)
    if not (-90 < event.latitude < 90 and -180 <= event.longitude <= 180):
        raise ValueError("coordinates outside valid bounds")
    if event.event_id in (".", "..") or any(char in event.event_id for char in '/\\:'):
        raise ValueError("invalid event id")
    return event


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

    if requested_ids:
        limit = args.limit
    else:
        limit = (
            MULTIMODAL_BATCH_DEFAULT_LIMIT
            if args.limit is None
            else args.limit
        )

    if limit is not None and limit > 0:
        selected_rows = selected_rows[:limit]

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


def build_config(args: argparse.Namespace) -> MultimodalSatelliteConfig:
    return MultimodalSatelliteConfig(
        aoi_half_size_km=args.aoi_half_size_km,
        window_days=args.window_days,
        output_resolution_m=args.output_resolution_m,
        s3_resolution_m=args.s3_resolution_m,
        max_cloud_cover=args.max_cloud_cover,
        include_png_previews=not args.no_png_previews,
        include_land_cover=not args.skip_land_cover,
        include_sentinel_3=not args.skip_sentinel_3,
    )


def load_manifest(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_path_for(output_dir: str, event: SatelliteEvent) -> Path:
    return Path(output_dir) / event.event_id / "manifest.json"


def count_output_files(manifest: Dict) -> int:
    count = 0
    for day in manifest.get("days", []):
        for sensor_name in ["sentinel_2", "sentinel_1", "sentinel_3_slstr"]:
            count += len(day.get(sensor_name, {}).get("outputs", {}))

    land_cover = manifest.get("land_cover") or {}
    count += len(land_cover.get("outputs", {}))
    return count


def nested_get(data: Dict, path: Iterable[str], default=""):
    current = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def has_any_satellite_data(available_days: Dict) -> bool:
    return any(
        int(available_days.get(sensor_name) or 0) > 0
        for sensor_name in ["sentinel_2", "sentinel_1", "sentinel_3_slstr"]
    )


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
    quality = manifest.get("quality_summary", {})
    land_cover = manifest.get("land_cover") or {}
    available_days = quality.get("available_days", {})
    missing_days = quality.get("missing_days", {})
    error_days = quality.get("error_days", {})

    if manifest_path is None and manifest.get("manifest_path"):
        manifest_path = Path(manifest["manifest_path"])

    return {
        "run_started_at": run_started_at,
        "event_id": event.event_id,
        "country": event.country or "",
        "disaster_type": event.disaster_type or "",
        "location": event.location or "",
        "start_date": event.start_date,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "status": status,
        "manifest_path": str(manifest_path or ""),
        "total_days": quality.get("total_days", ""),
        "sentinel_2_available_days": available_days.get("sentinel_2", ""),
        "sentinel_1_available_days": available_days.get("sentinel_1", ""),
        "sentinel_3_slstr_available_days": available_days.get(
            "sentinel_3_slstr",
            "",
        ),
        "sentinel_2_missing_days": missing_days.get("sentinel_2", ""),
        "sentinel_1_missing_days": missing_days.get("sentinel_1", ""),
        "sentinel_3_slstr_missing_days": missing_days.get("sentinel_3_slstr", ""),
        "land_cover_available": land_cover.get("available", ""),
        "land_cover_year": land_cover.get("year", ""),
        "land_cover_product": land_cover.get("product", ""),
        "land_cover_status": land_cover.get("status", ""),
        "sentinel_2_error_days": error_days.get("sentinel_2", ""),
        "sentinel_1_error_days": error_days.get("sentinel_1", ""),
        "sentinel_3_slstr_error_days": error_days.get("sentinel_3_slstr", ""),
        "manifest_status": manifest.get("status", ""),
        "has_any_satellite_data": has_any_satellite_data(available_days),
        "output_file_count": count_output_files(manifest),
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
    if args.event_ids and args.limit is None:
        limit_label = "all requested event ids"
    elif args.limit is None:
        limit_label = MULTIMODAL_BATCH_DEFAULT_LIMIT
    else:
        limit_label = args.limit

    print(f"Dry run selected {len(events)} event(s). No API calls will be made.")
    print(
        "Filters: "
        f"disaster_type={args.disaster_type!r}, "
        f"country={args.country!r}, "
        f"min_start_date={args.min_start_date!r}, "
        f"limit={limit_label!r}"
    )
    print("Selected events by type:")
    for disaster_type, count in sorted(Counter(event.disaster_type or "unknown" for event in events).items()):
        print(f"  {disaster_type}: {count}")
    for index, event in enumerate(events[:10], start=1):
        print(
            f"{index}. {event.event_id} | {event.disaster_type or '-'} | "
            f"{event.country or '-'} | {event.start_date} | "
            f"lat={event.latitude:.5f}, lon={event.longitude:.5f} | "
            f"{event.location or '-'}"
        )


def run_batch(args: argparse.Namespace) -> None:
    events = select_events(args)
    config = build_config(args)
    if args.sleep_seconds < 0 or args.min_free_disk_gb < 0:
        raise SystemExit("Sleep and free-disk reserve must be non-negative")
    if args.dry_run:
        print_dry_run(events, args)
        return

    require_copernicus_credentials()
    run_started_at = datetime.now().isoformat(timespec="seconds")
    print(f"Selected {len(events)} event(s). Summary: {args.summary_csv}")
    counts = Counter()
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    for index, event in enumerate(events, start=1):
        start = time.perf_counter()
        manifest_path = manifest_path_for(args.output_dir, event)
        print(f"[{index}/{len(events)}] {event.event_id}: starting")
        manifest = None
        try:
            if manifest_path.exists() and not args.force:
                manifest = load_manifest(manifest_path)
                if not manifest_matches(manifest, event, config):
                    raise SatelliteConfigurationError("Existing configuration differs; choose a new output directory or --force")
            if manifest and manifest_complete(manifest):
                status = "skipped_existing"
                print(f"[{index}/{len(events)}] {event.event_id}: complete checkpoint, skipped")
            else:
                free_gb = shutil.disk_usage(args.output_dir).free / (1024 ** 3)
                if free_gb < args.min_free_disk_gb:
                    raise OSError(f"Only {free_gb:.1f} GiB free; batch stopped to preserve disk space")
                manifest = run_multimodal_satellite_event(
                    event,
                    config,
                    output_dir=args.output_dir,
                    force=args.force,
                )
                manifest_path = Path(manifest["manifest_path"])
                status = manifest["status"]
                quality = manifest.get("quality_summary", {})
                print(
                    f"[{index}/{len(events)}] {event.event_id}: {status} "
                    f"(S2={nested_get(quality, ['available_days', 'sentinel_2'], 0)}, "
                    f"S1={nested_get(quality, ['available_days', 'sentinel_1'], 0)}, "
                    f"S3={nested_get(quality, ['available_days', 'sentinel_3_slstr'], 0)})"
                )

            elapsed = time.perf_counter() - start
            counts[status] += 1
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
            counts["error"] += 1
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
                    manifest=load_manifest(manifest_path) if manifest_path.exists() else None,
                    manifest_path=manifest_path,
                    error=error,
                ),
            )
            if (args.stop_on_error or isinstance(exc, (SatelliteConfigurationError, OSError))
                    or isinstance(exc, SentinelHubRequestError) and exc.status_code in (401, 403, 429)):
                raise SystemExit(error) from exc

        if index < len(events) and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    print("Batch finished:", dict(counts))
    print(f"Summary CSV: {args.summary_csv}")
    if counts["partial"] or counts["error"]:
        raise SystemExit("Some downloads failed; repeat the same command to retry incomplete records.")


def main() -> None:
    run_batch(parse_args())


if __name__ == "__main__":
    main()
