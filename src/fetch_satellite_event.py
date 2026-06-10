import argparse
import csv
import os
from typing import Dict, Optional

from support.satellite_engine import (
    SATELLITE_OUTPUT_DIR,
    SatelliteEvent,
    SatelliteRunConfig,
    run_satellite_event,
)


DEFAULT_EVENTS_CSV = os.path.join("results", "disasters_per_satellite.csv")


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
            "Download first-pass Sentinel-1/Sentinel-2 flood layers for one event "
            "using Copernicus Data Space Sentinel Hub APIs."
        )
    )
    parser.add_argument("--event-id", help="EM-DAT disaster id, e.g. 2018-0040-BRA")
    parser.add_argument("--events-csv", default=DEFAULT_EVENTS_CSV)
    parser.add_argument("--lat", type=float, help="Manual latitude if event-id is not in CSV")
    parser.add_argument("--lon", type=float, help="Manual longitude if event-id is not in CSV")
    parser.add_argument("--start-date", help="Manual start date YYYY-MM-DD")
    parser.add_argument("--country")
    parser.add_argument("--disaster-type")
    parser.add_argument("--location")
    parser.add_argument("--output-dir", default=SATELLITE_OUTPUT_DIR)
    parser.add_argument("--aoi-half-size-km", type=float, default=10.0)
    parser.add_argument("--window-days", type=int, default=10)
    parser.add_argument("--image-size", type=int, default=768)
    parser.add_argument("--max-cloud-cover", type=float, default=30.0)
    parser.add_argument("--s2-cloud-eval-size", type=int, default=128)
    parser.add_argument("--s2-cloud-candidate-limit", type=int, default=8)
    parser.add_argument("--s2-usable-local-cloud-cover", type=float, default=30.0)
    parser.add_argument("--s2-water-threshold", type=float, default=0.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    event = event_from_args(args)
    config = SatelliteRunConfig(
        aoi_half_size_km=args.aoi_half_size_km,
        window_days=args.window_days,
        image_size=args.image_size,
        max_cloud_cover=args.max_cloud_cover,
        s2_cloud_eval_size=args.s2_cloud_eval_size,
        s2_cloud_candidate_limit=args.s2_cloud_candidate_limit,
        s2_usable_local_cloud_cover=args.s2_usable_local_cloud_cover,
        s2_water_threshold=args.s2_water_threshold,
    )

    try:
        manifest = run_satellite_event(event, config, output_dir=args.output_dir)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print("Satellite extraction completed.")
    print(f"Manifest: {manifest['manifest_path']}")
    for name, path in manifest["outputs"].items():
        print(f"{name}: {path}")

    missing = [key for key, value in manifest["scenes"].items() if value is None]
    if missing:
        print("Warning: no suitable acquisition found for: " + ", ".join(missing))


if __name__ == "__main__":
    main()
