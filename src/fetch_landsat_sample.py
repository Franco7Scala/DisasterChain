import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

from probe_landsat_endpoint import (
    LANDSAT_COLLECTIONS,
    endpoint_urls,
    event_from_args,
    print_items,
    print_query,
    require_probe_credentials,
)
from support.constants import (
    CLEANED_DISASTERS_OUTPUT_PATH,
    LANDSAT_OT_L2_RAW_BANDS,
    LANDSAT_PROBE_OUTPUT_DIR,
    SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    SATELLITE_DEFAULT_TIMEOUT_SECONDS,
    SATELLITE_DEFAULT_WINDOW_DAYS,
    SENTINEL_CATALOG_SEARCH_LIMIT,
)
from support.multimodal_satellite_engine import (
    process_payload_by_resolution,
)
from support.satellite_engine import (
    SatelliteEvent,
    SentinelHubClient,
    SentinelHubRequestError,
    date_window,
    event_bbox,
    parse_catalog_datetime,
)


LANDSAT_TRUE_COLOR_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B02", "B03", "B04", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "UINT16" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  return [scale(sample.B04), scale(sample.B03), scale(sample.B02)];
}

function scale(value) {
  return Math.round(Math.max(0, Math.min(1, value * 2.5)) * 65535);
}
"""


LANDSAT_FALSE_COLOR_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B04", "B05", "B07", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "UINT16" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  return [scale(sample.B07), scale(sample.B05), scale(sample.B04)];
}

function scale(value) {
  return Math.round(Math.max(0, Math.min(1, value * 2.5)) * 65535);
}
"""


LANDSAT_TRUE_COLOR_PREVIEW_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B02", "B03", "B04", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "AUTO" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  return [2.5 * sample.B04, 2.5 * sample.B03, 2.5 * sample.B02];
}
"""


LANDSAT_FALSE_COLOR_PREVIEW_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B04", "B05", "B07", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "AUTO" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  return [2.5 * sample.B07, 2.5 * sample.B05, 2.5 * sample.B04];
}
"""


LANDSAT_RAW_BANDS_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: LANDSAT_RAW_BANDS,
    output: { id: "default", bands: LANDSAT_RAW_BAND_COUNT, sampleType: "FLOAT32" }
  };
}

function evaluatePixel(sample) {
  return [
    sample.B01,
    sample.B02,
    sample.B03,
    sample.B04,
    sample.B05,
    sample.B06,
    sample.B07
  ];
}
""".replace(
    "LANDSAT_RAW_BANDS",
    json.dumps(LANDSAT_OT_L2_RAW_BANDS),
).replace(
    "LANDSAT_RAW_BAND_COUNT",
    str(len(LANDSAT_OT_L2_RAW_BANDS)),
)


LANDSAT_THERMAL_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B10", "dataMask"],
    output: { id: "default", bands: 1, sampleType: "FLOAT32" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [-9999];
  }
  return [sample.B10];
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download one small Landsat sample after the catalog endpoint probe "
            "has succeeded."
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
        choices=["landsat-ot-l2"],
        default="landsat-ot-l2",
        help="Initial sample supports Landsat 8-9 L2.",
    )
    parser.add_argument(
        "--deployment",
        choices=["uswest"],
        default="uswest",
        help="Landsat L2 is queried on the US-West deployment.",
    )
    parser.add_argument(
        "--output-dir",
        default=LANDSAT_PROBE_OUTPUT_DIR,
    )
    parser.add_argument(
        "--aoi-half-size-km",
        type=float,
        default=SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    )
    parser.add_argument("--window-days", type=int, default=SATELLITE_DEFAULT_WINDOW_DAYS)
    parser.add_argument("--output-resolution-m", type=int, default=30)
    parser.add_argument("--max-cloud-cover", type=float, default=100.0)
    parser.add_argument("--limit", type=int, default=SENTINEL_CATALOG_SEARCH_LIMIT)
    parser.add_argument("--timeout-seconds", type=int, default=SATELLITE_DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print query parameters without API calls.",
    )
    return parser.parse_args()


def selected_scene(items: List[Dict]) -> Dict:
    if not items:
        raise RuntimeError("No Landsat scenes found for this query.")
    return sorted(
        items,
        key=lambda item: (
            float(item.get("properties", {}).get("eo:cloud_cover") or 100.0),
            parse_catalog_datetime(item.get("properties", {}).get("datetime", "")),
        ),
    )[0]


def day_time_range(scene: Dict) -> Dict[str, str]:
    day = scene["properties"]["datetime"][:10]
    return {
        "from": f"{day}T00:00:00Z",
        "to": f"{day}T23:59:59Z",
        "day": day,
    }


def process_output(
    client: SentinelHubClient,
    collection: str,
    bbox: List[float],
    time_range: Dict[str, str],
    evalscript: str,
    resolution_m: int,
    output_path: Path,
    output_format: str,
) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = process_payload_by_resolution(
        collection,
        bbox,
        time_range["from"],
        time_range["to"],
        evalscript,
        resolution_m,
        output_format,
        data_filter={
            "maxCloudCoverage": 100,
            "mosaickingOrder": "leastCC",
            "tiers": "TIER_1",
        },
        processing={"upsampling": "BILINEAR", "downsampling": "BILINEAR"},
    )
    client.process_image(payload, output_path, accept=output_format)
    return str(output_path)


def download_sample_outputs(
    client: SentinelHubClient,
    collection: str,
    event: SatelliteEvent,
    bbox: List[float],
    scene: Dict,
    output_dir: str,
    resolution_m: int,
) -> Dict:
    time_range = day_time_range(scene)
    output_root = Path(output_dir) / event.event_id / time_range["day"]
    outputs = {
        "true_color_tif": process_output(
            client,
            collection,
            bbox,
            time_range,
            LANDSAT_TRUE_COLOR_TIFF_EVALSCRIPT,
            resolution_m,
            output_root / "true_color.tif",
            "image/tiff",
        ),
        "false_color_tif": process_output(
            client,
            collection,
            bbox,
            time_range,
            LANDSAT_FALSE_COLOR_TIFF_EVALSCRIPT,
            resolution_m,
            output_root / "false_color.tif",
            "image/tiff",
        ),
        "raw_bands_tif": process_output(
            client,
            collection,
            bbox,
            time_range,
            LANDSAT_RAW_BANDS_TIFF_EVALSCRIPT,
            resolution_m,
            output_root / "raw_bands.tif",
            "image/tiff",
        ),
        "thermal_bands_tif": process_output(
            client,
            collection,
            bbox,
            time_range,
            LANDSAT_THERMAL_TIFF_EVALSCRIPT,
            resolution_m,
            output_root / "thermal_bands.tif",
            "image/tiff",
        ),
        "true_color_preview_png": process_output(
            client,
            collection,
            bbox,
            time_range,
            LANDSAT_TRUE_COLOR_PREVIEW_EVALSCRIPT,
            resolution_m,
            output_root / "true_color_preview.png",
            "image/png",
        ),
        "false_color_preview_png": process_output(
            client,
            collection,
            bbox,
            time_range,
            LANDSAT_FALSE_COLOR_PREVIEW_EVALSCRIPT,
            resolution_m,
            output_root / "false_color_preview.png",
            "image/png",
        ),
    }
    manifest = {
        "schema_version": "landsat-probe-v1",
        "event": asdict(event),
        "bbox": bbox,
        "collection": collection,
        "collection_label": LANDSAT_COLLECTIONS[collection],
        "output_resolution_m": resolution_m,
        "selected_scene": {
            "id": scene.get("id"),
            "datetime": scene.get("properties", {}).get("datetime"),
            "cloud_cover": scene.get("properties", {}).get("eo:cloud_cover"),
            "landsat_scene_id": scene.get("properties", {}).get("landsat:scene_id"),
            "landsat_collection_category": scene.get("properties", {}).get(
                "landsat:collection_category"
            ),
        },
        "outputs": outputs,
        "notes": {
            "raw_bands_tif": f"Band order: {', '.join(LANDSAT_OT_L2_RAW_BANDS)}.",
            "thermal_bands_tif": "Band order: B10 surface temperature source band.",
            "false_color_tif": "Composite order: SWIR2, NIR, Red.",
        },
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


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
        print(f"Output directory: {args.output_dir}")
        print(f"Output resolution: {args.output_resolution_m} m")
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
        print_items(items[:5])
        scene = selected_scene(
            [
                item
                for item in items
                if float(item.get("properties", {}).get("eo:cloud_cover") or 100.0)
                <= args.max_cloud_cover
            ]
        )
        manifest = download_sample_outputs(
            client,
            args.collection,
            event,
            bbox,
            scene,
            args.output_dir,
            args.output_resolution_m,
        )
    except (RuntimeError, SentinelHubRequestError) as exc:
        raise SystemExit(f"Landsat sample download failed.\n{exc}") from exc

    print("Landsat sample download completed.")
    print(f"Manifest: {manifest['manifest_path']}")
    for name, path in manifest["outputs"].items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
