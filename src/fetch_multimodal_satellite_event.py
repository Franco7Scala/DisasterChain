import argparse
import csv
import os
from typing import Dict, Optional

from support.constants import (
    CLEANED_DISASTERS_OUTPUT_PATH,
    MULTIMODAL_DEFAULT_MAX_CLOUD_COVER,
    MULTIMODAL_DEFAULT_OUTPUT_RESOLUTION_M,
    MULTIMODAL_DEFAULT_S3_RESOLUTION_M,
    MULTIMODAL_SATELLITE_OUTPUT_DIR,
    SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    SATELLITE_DEFAULT_WINDOW_DAYS,
)
from support.multimodal_satellite_engine import (
    MultimodalSatelliteConfig,
    analysis_dates,
    run_multimodal_satellite_event,
)
from support.satellite_engine import SatelliteEvent, event_bbox


def load_event_from_csv(path: str, event_id: str) -> Optional[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("emdat_disaster_id") == event_id:
                return row
    return None


def event_from_args(args: argparse.Namespace) -> SatelliteEvent:
    row = None
    if args.event_id and os.path.exists(args.events_csv):
        row = load_event_from_csv(args.events_csv, args.event_id)

    if row:
        return SatelliteEvent(
            event_id=row["emdat_disaster_id"],
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            start_date=row["start_date"],
            country=row.get("country") or None,
            disaster_type=row.get("disaster_type") or None,
            location=row.get("location") or row.get("emdat_location") or None,
        )

    missing = [
        name
        for name, value in [
            ("--event-id", args.event_id),
            ("--lat", args.lat),
            ("--lon", args.lon),
            ("--start-date", args.start_date),
        ]
        if value is None
    ]
    if missing:
        raise SystemExit(
            "Event not found in CSV and manual coordinates are incomplete. "
            f"Missing: {', '.join(missing)}"
        )

    return SatelliteEvent(
        event_id=args.event_id,
        latitude=float(args.lat),
        longitude=float(args.lon),
        start_date=args.start_date,
        country=args.country,
        disaster_type=args.disaster_type,
        location=args.location,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download the general 21-day multimodal satellite layer for one "
            "disaster event."
        )
    )
    parser.add_argument("--event-id", help="EM-DAT disaster id, e.g. 2018-0040-BRA")
    parser.add_argument("--events-csv", default=CLEANED_DISASTERS_OUTPUT_PATH)
    parser.add_argument("--lat", type=float, help="Manual latitude if event-id is not in CSV")
    parser.add_argument("--lon", type=float, help="Manual longitude if event-id is not in CSV")
    parser.add_argument("--start-date", help="Manual start date YYYY-MM-DD")
    parser.add_argument("--country")
    parser.add_argument("--disaster-type")
    parser.add_argument("--location")
    parser.add_argument("--output-dir", default=MULTIMODAL_SATELLITE_OUTPUT_DIR)
    parser.add_argument(
        "--aoi-half-size-km",
        type=float,
        default=SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
        help="Half-size of the fixed bbox centered on the event coordinates.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=SATELLITE_DEFAULT_WINDOW_DAYS,
        help="Days before/after event date. Default 10 gives 21 days total.",
    )
    parser.add_argument(
        "--output-resolution-m",
        type=int,
        default=MULTIMODAL_DEFAULT_OUTPUT_RESOLUTION_M,
        help="Output grid resolution for Sentinel-1, Sentinel-2, and land cover.",
    )
    parser.add_argument(
        "--s3-resolution-m",
        type=int,
        default=MULTIMODAL_DEFAULT_S3_RESOLUTION_M,
        help="Output grid resolution for Sentinel-3 SLSTR thermal bands.",
    )
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=MULTIMODAL_DEFAULT_MAX_CLOUD_COVER,
        help="Tile-level Sentinel-2 cloud-cover ceiling. Default keeps all scenes.",
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print event, bbox, and 21-day window without calling APIs.",
    )
    return parser.parse_args()


def print_dry_run(event: SatelliteEvent, config: MultimodalSatelliteConfig) -> None:
    dates = analysis_dates(event.start_date, config.window_days)
    bbox = event_bbox(event.latitude, event.longitude, config.aoi_half_size_km)
    print("Dry run. No API calls will be made.")
    print(f"Event: {event.event_id} | {event.country or '-'} | {event.start_date}")
    print(f"Location: {event.location or '-'}")
    print(f"Disaster type: {event.disaster_type or '-'}")
    print(f"Bbox: {bbox}")
    print(f"Dates: {dates[0]} to {dates[-1]} ({len(dates)} days)")
    print(f"S1/S2/Land-cover resolution: {config.output_resolution_m} m")
    print(f"Sentinel-3 SLSTR resolution: {config.s3_resolution_m} m")


def main() -> None:
    args = parse_args()
    event = event_from_args(args)
    config = MultimodalSatelliteConfig(
        aoi_half_size_km=args.aoi_half_size_km,
        window_days=args.window_days,
        output_resolution_m=args.output_resolution_m,
        s3_resolution_m=args.s3_resolution_m,
        max_cloud_cover=args.max_cloud_cover,
        include_png_previews=not args.no_png_previews,
        include_land_cover=not args.skip_land_cover,
        include_sentinel_3=not args.skip_sentinel_3,
    )

    if args.dry_run:
        print_dry_run(event, config)
        return

    try:
        manifest = run_multimodal_satellite_event(
            event,
            config,
            output_dir=args.output_dir,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print("Multimodal satellite extraction completed.")
    print(f"Manifest: {manifest['manifest_path']}")
    quality = manifest["quality_summary"]
    for sensor_name, available_days in quality["available_days"].items():
        missing_days = quality["missing_days"][sensor_name]
        print(f"{sensor_name}: {available_days} available day(s), {missing_days} missing")
    if manifest.get("land_cover"):
        land_cover = manifest["land_cover"]
        print(
            "land_cover: "
            f"{'available' if land_cover['available'] else 'missing'} "
            f"(year={land_cover.get('year')})"
        )


if __name__ == "__main__":
    main()
