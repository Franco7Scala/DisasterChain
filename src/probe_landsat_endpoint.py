import argparse
import csv
import os
from typing import Dict, List, Optional

from support.constants import (
    CATALOG_SEARCH_URL,
    CLEANED_DISASTERS_OUTPUT_PATH,
    COPERNICUS_AUTH_URL,
    PROCESS_URL,
    SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    SATELLITE_DEFAULT_TIMEOUT_SECONDS,
    SATELLITE_DEFAULT_WINDOW_DAYS,
    SENTINEL_HUB_MAIN_AUTH_URL,
    SENTINEL_HUB_USWEST_CATALOG_SEARCH_URL,
    SENTINEL_HUB_USWEST_PROCESS_URL,
)
from support.satellite_engine import (
    SatelliteEvent,
    SentinelHubClient,
    SentinelHubRequestError,
    date_window,
    event_bbox,
)


LANDSAT_COLLECTIONS = {
    "landsat-ot-l2": "Landsat 8-9 OLI-TIRS L2",
    "landsat-etm-l2": "Landsat 7 ETM+ L2",
    "landsat-tm-l2": "Landsat 4-5 TM L2",
}


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


def endpoint_urls(deployment: str) -> Dict[str, str]:
    if deployment == "uswest":
        return {
            "auth_url": SENTINEL_HUB_MAIN_AUTH_URL,
            "catalog_search_url": SENTINEL_HUB_USWEST_CATALOG_SEARCH_URL,
            "process_url": SENTINEL_HUB_USWEST_PROCESS_URL,
        }

    return {
        "auth_url": COPERNICUS_AUTH_URL,
        "catalog_search_url": CATALOG_SEARCH_URL,
        "process_url": PROCESS_URL,
    }


def require_probe_credentials(deployment: str) -> Dict[str, str]:
    if deployment == "uswest":
        client_id = os.environ.get("SH_CLIENT_ID") or os.environ.get("COPERNICUS_CLIENT_ID")
        client_secret = os.environ.get("SH_CLIENT_SECRET") or os.environ.get(
            "COPERNICUS_CLIENT_SECRET"
        )
        expected = "SH_CLIENT_ID/SH_CLIENT_SECRET or COPERNICUS_CLIENT_ID/COPERNICUS_CLIENT_SECRET"
    else:
        client_id = os.environ.get("COPERNICUS_CLIENT_ID") or os.environ.get("SH_CLIENT_ID")
        client_secret = os.environ.get("COPERNICUS_CLIENT_SECRET") or os.environ.get(
            "SH_CLIENT_SECRET"
        )
        expected = "COPERNICUS_CLIENT_ID/COPERNICUS_CLIENT_SECRET or SH_CLIENT_ID/SH_CLIENT_SECRET"

    if not client_id or not client_secret:
        raise RuntimeError(f"Missing credentials. Set {expected} before running this probe.")

    return {"client_id": client_id, "client_secret": client_secret}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Landsat catalog availability before adding Landsat downloads "
            "to the multimodal satellite pipeline."
        )
    )
    parser.add_argument("--event-id", help="EM-DAT disaster id")
    parser.add_argument("--events-csv", default=CLEANED_DISASTERS_OUTPUT_PATH)
    parser.add_argument("--lat", type=float, help="Manual latitude if event-id is not in CSV")
    parser.add_argument("--lon", type=float, help="Manual longitude if event-id is not in CSV")
    parser.add_argument("--start-date", help="Manual start date YYYY-MM-DD")
    parser.add_argument("--country")
    parser.add_argument("--disaster-type")
    parser.add_argument("--location")
    parser.add_argument(
        "--collection",
        choices=sorted(LANDSAT_COLLECTIONS),
        default="landsat-ot-l2",
        help="Landsat collection to query.",
    )
    parser.add_argument(
        "--deployment",
        choices=["uswest", "cdse"],
        default="uswest",
        help="Catalog deployment to query. Landsat L2 is expected on uswest.",
    )
    parser.add_argument(
        "--aoi-half-size-km",
        type=float,
        default=SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    )
    parser.add_argument("--window-days", type=int, default=SATELLITE_DEFAULT_WINDOW_DAYS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=int, default=SATELLITE_DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print query parameters without calling Sentinel Hub.",
    )
    return parser.parse_args()


def print_query(
    event: SatelliteEvent,
    bbox: List[float],
    from_date: str,
    to_date: str,
    args: argparse.Namespace,
    urls: Dict[str, str],
) -> None:
    print(f"Event: {event.event_id} | {event.disaster_type or '-'} | {event.country or '-'}")
    print(f"Date window: {from_date} to {to_date}")
    print(f"Bbox: {bbox}")
    print(f"Collection: {args.collection} ({LANDSAT_COLLECTIONS[args.collection]})")
    print(f"Deployment: {args.deployment}")
    print(f"Auth URL: {urls['auth_url']}")
    print(f"Catalog URL: {urls['catalog_search_url']}")


def print_items(items: List[Dict]) -> None:
    if not items:
        print("No Landsat scenes found for this query.")
        return

    print(f"Found {len(items)} Landsat scene(s). Showing first {len(items)}:")
    for index, item in enumerate(items, start=1):
        properties = item.get("properties", {})
        scene_id = properties.get("landsat:scene_id") or "-"
        category = properties.get("landsat:collection_category") or "-"
        cloud_cover = properties.get("eo:cloud_cover")
        print(
            f"{index}. {item.get('id', '-')} | "
            f"{properties.get('datetime', '-')} | "
            f"cloud={cloud_cover if cloud_cover is not None else '-'} | "
            f"tier={category} | scene={scene_id}"
        )


def main() -> None:
    args = parse_args()
    event = event_from_args(args)
    bbox = event_bbox(event.latitude, event.longitude, args.aoi_half_size_km)
    from_date, to_date = date_window(
        event.start_date,
        args.window_days,
        args.window_days,
    )
    urls = endpoint_urls(args.deployment)

    if args.dry_run:
        print("Dry run. No API calls will be made.")
        print_query(event, bbox, from_date, to_date, args, urls)
        return

    credentials = require_probe_credentials(args.deployment)
    client = SentinelHubClient(
        credentials["client_id"],
        credentials["client_secret"],
        timeout_seconds=args.timeout_seconds,
        auth_url=urls["auth_url"],
        catalog_search_url=urls["catalog_search_url"],
        process_url=urls["process_url"],
    )

    print_query(event, bbox, from_date, to_date, args, urls)
    try:
        items = client.catalog_search(
            args.collection,
            bbox,
            from_date,
            to_date,
            limit=args.limit,
        )
    except SentinelHubRequestError as exc:
        raise SystemExit(
            f"Landsat endpoint probe failed.\n{exc}\n\n"
            "If this is a 401 on services.sentinel-hub.com, the current "
            "Copernicus Data Space credentials are probably not valid for the "
            "general Sentinel Hub deployment. Create/use Sentinel Hub OAuth "
            "credentials and expose them as SH_CLIENT_ID and SH_CLIENT_SECRET."
        ) from exc
    print_items(items)


if __name__ == "__main__":
    main()
