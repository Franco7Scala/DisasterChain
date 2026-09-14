import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from support.constants import (
    MULTIMODAL_DEFAULT_MAX_CLOUD_COVER,
    MULTIMODAL_DEFAULT_OUTPUT_RESOLUTION_M,
    MULTIMODAL_DEFAULT_S3_RESOLUTION_M,
    MULTIMODAL_S2_RAW_BANDS,
    MULTIMODAL_S3_THERMAL_BANDS,
    MULTIMODAL_SATELLITE_OUTPUT_DIR,
    MULTIMODAL_SCHEMA_VERSION,
    SATELLITE_DEFAULT_AOI_HALF_SIZE_KM,
    SATELLITE_DEFAULT_TIMEOUT_SECONDS,
    SATELLITE_DEFAULT_WINDOW_DAYS,
    SENTINEL_CATALOG_SEARCH_LIMIT,
)
from support.worldcover import download_worldcover
from support.satellite_lock import satellite_output_lock
from support.satellite_rendering import (
    RENDERING_VERSION,
    download_raw_bundle,
    render_s1_layers,
    render_s2_layers,
)
from support.satellite_engine import (
    SatelliteEvent,
    SentinelHubClient,
    SentinelHubRequestError,
    event_bbox,
    parse_catalog_datetime,
    require_copernicus_credentials,
)


class SatelliteConfigurationError(ValueError):
    pass


@dataclass
class MultimodalSatelliteConfig:
    aoi_half_size_km: float = SATELLITE_DEFAULT_AOI_HALF_SIZE_KM
    window_days: int = SATELLITE_DEFAULT_WINDOW_DAYS
    output_resolution_m: int = MULTIMODAL_DEFAULT_OUTPUT_RESOLUTION_M
    s3_resolution_m: int = MULTIMODAL_DEFAULT_S3_RESOLUTION_M
    max_cloud_cover: float = MULTIMODAL_DEFAULT_MAX_CLOUD_COVER
    timeout_seconds: int = SATELLITE_DEFAULT_TIMEOUT_SECONDS
    include_png_previews: bool = True
    include_land_cover: bool = True
    include_sentinel_3: bool = True

    # Rejects configurations that cannot produce valid image requests.
    def __post_init__(self):
        if self.window_days < 0 or self.aoi_half_size_km <= 0:
            raise ValueError("Window must be non-negative and area size must be positive")
        if self.output_resolution_m <= 0 or self.s3_resolution_m <= 0:
            raise ValueError("Output resolutions must be positive")
        if not 0 <= self.max_cloud_cover <= 100:
            raise ValueError("Cloud cover must be between 0 and 100")
        if 2000 * self.aoi_half_size_km / min(self.output_resolution_m, self.s3_resolution_m) > 2500:
            raise ValueError("Requested image exceeds the 2500-pixel Process API limit")


class DailyCatalogClient(SentinelHubClient):
    # Fetches the full event window once per sensor, including every catalog page.
    def __init__(self, client_id, client_secret, timeout_seconds, dates):
        super().__init__(client_id, client_secret, timeout_seconds)
        self.dates = dates
        self.catalog_cache = {}

    # Returns cached acquisitions for the requested day and collection.
    def catalog_search(self, collection, bbox, from_date, to_date, limit=100):
        key = (collection, tuple(bbox))
        if key not in self.catalog_cache:
            self.catalog_cache[key] = super().catalog_search(
                collection, bbox, self.dates[0], self.dates[-1], limit, all_pages=True,
            )
        return [item for item in self.catalog_cache[key]
                if from_date <= _scene_datetime(item)[:10] <= to_date]


S2_RAW_BUNDLE_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: S2_RAW_BANDS,
    output: [
      { id: "default", bands: S2_RAW_BAND_COUNT, sampleType: "FLOAT32" },
      { id: "data_mask", bands: 1, sampleType: "UINT8" }
    ]
  };
}

function evaluatePixel(sample) {
  return { default: [
    sample.B02,
    sample.B03,
    sample.B04,
    sample.B05,
    sample.B06,
    sample.B07,
    sample.B08,
    sample.B8A,
    sample.B11,
    sample.B12
  ], data_mask: [sample.dataMask] };
}
""".replace(
    "S2_RAW_BANDS",
    json.dumps(MULTIMODAL_S2_RAW_BANDS + ["dataMask"]),
).replace(
    "S2_RAW_BAND_COUNT",
    str(len(MULTIMODAL_S2_RAW_BANDS)),
)


S1_RAW_BUNDLE_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["VV", "VH", "dataMask"],
    output: [
      { id: "default", bands: 2, sampleType: "FLOAT32" },
      { id: "data_mask", bands: 1, sampleType: "UINT8" }
    ]
  };
}

function evaluatePixel(sample) {
  return {
    default: sample.dataMask === 0 ? [0, 0] : [sample.VV, sample.VH],
    data_mask: [sample.dataMask]
  };
}
"""


S3_THERMAL_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: [{
      bands: S3_THERMAL_BANDS,
      units: "BRIGHTNESS_TEMPERATURE"
    }],
    output: { id: "default", bands: S3_THERMAL_BAND_COUNT, sampleType: "FLOAT32" }
  };
}

function evaluatePixel(sample) {
  return [sample.S7, sample.S8, sample.S9, sample.F1, sample.F2];
}
""".replace(
    "S3_THERMAL_BANDS",
    json.dumps(MULTIMODAL_S3_THERMAL_BANDS),
).replace(
    "S3_THERMAL_BAND_COUNT",
    str(len(MULTIMODAL_S3_THERMAL_BANDS)),
)


def analysis_dates(start_date: str, window_days: int) -> List[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    return [
        (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(-window_days, window_days + 1)
    ]


def day_time_range(day: str) -> Tuple[str, str]:
    return f"{day}T00:00:00Z", f"{day}T23:59:59Z"


# Restricts processing to the acquisition chosen from the catalog.
def acquisition_time_range(scene: Dict) -> Tuple[str, str]:
    acquisition = parse_catalog_datetime(_scene_datetime(scene))
    return tuple((acquisition + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
                 for offset in (-1, 1))


def bbox_dimensions_km(bbox: List[float]) -> Tuple[float, float]:
    west, south, east, north = bbox
    avg_latitude = (south + north) / 2
    width_km = (
        abs(east - west)
        * 111.32
        * max(math.cos(math.radians(avg_latitude)), 0.01)
    )
    height_km = abs(north - south) * 111.32
    return width_km, height_km


def image_dimensions_for_resolution(
    bbox: List[float],
    resolution_m: int,
) -> Tuple[int, int]:
    width_km, height_km = bbox_dimensions_km(bbox)
    width = max(1, round((width_km * 1000) / resolution_m))
    height = max(1, round((height_km * 1000) / resolution_m))
    return width, height


def _scene_datetime(item: Dict) -> str:
    return item.get("properties", {}).get("datetime", "")


def scene_record(item: Optional[Dict]) -> Optional[Dict]:
    if item is None:
        return None
    properties = item.get("properties", {})
    return {
        "item_id": item.get("id", ""),
        "datetime": properties.get("datetime"),
        "cloud_cover": properties.get("eo:cloud_cover"),
        "polarization": properties.get("s1:polarization"),
        "instrument_mode": properties.get("sar:instrument_mode"),
        "orbit_state": properties.get("sat:orbit_state"),
    }


def _sort_by_datetime(items: List[Dict]) -> List[Dict]:
    return sorted(items, key=lambda item: parse_catalog_datetime(_scene_datetime(item)))


# Preserves zero cloud cover instead of treating it as missing metadata.
def scene_cloud_cover(item: Dict) -> float:
    value = item.get("properties", {}).get("eo:cloud_cover")
    return 100.0 if value is None else float(value)


def select_daily_s2_scene(
    client: SentinelHubClient,
    bbox: List[float],
    day: str,
    max_cloud_cover: float,
) -> Optional[Dict]:
    items = client.catalog_search(
        "sentinel-2-l2a",
        bbox,
        day,
        day,
        limit=SENTINEL_CATALOG_SEARCH_LIMIT,
    )
    candidates = []
    for item in items:
        cloud_cover = item.get("properties", {}).get("eo:cloud_cover")
        if cloud_cover is None or float(cloud_cover) <= max_cloud_cover:
            candidates.append(item)
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            scene_cloud_cover(item),
            parse_catalog_datetime(_scene_datetime(item)),
        ),
    )[0]


def select_daily_s1_scene(
    client: SentinelHubClient,
    bbox: List[float],
    day: str,
) -> Optional[Dict]:
    items = client.catalog_search(
        "sentinel-1-grd",
        bbox,
        day,
        day,
        limit=SENTINEL_CATALOG_SEARCH_LIMIT,
    )
    candidates = [
        item
        for item in items
        if item.get("properties", {}).get("sar:instrument_mode") == "IW"
        and item.get("properties", {}).get("s1:polarization") == "DV"
    ]
    return _sort_by_datetime(candidates)[0] if candidates else None


def select_daily_s3_scene(
    client: SentinelHubClient,
    bbox: List[float],
    day: str,
) -> Optional[Dict]:
    items = client.catalog_search(
        "sentinel-3-slstr",
        bbox,
        day,
        day,
        limit=SENTINEL_CATALOG_SEARCH_LIMIT,
    )
    if not items:
        return None
    return sorted(
        items,
        key=lambda item: (
            scene_cloud_cover(item),
            parse_catalog_datetime(_scene_datetime(item)),
        ),
    )[0]


def process_payload_by_resolution(
    collection: str,
    bbox: List[float],
    time_from: str,
    time_to: str,
    evalscript: str,
    resolution_m: int,
    output_format: str,
    data_filter: Optional[Dict] = None,
    processing: Optional[Dict] = None,
) -> Dict:
    width, height = image_dimensions_for_resolution(bbox, resolution_m)
    data_entry = {
        "type": collection,
        "dataFilter": {
            "timeRange": {
                "from": time_from,
                "to": time_to,
            }
        },
    }
    if data_filter:
        data_entry["dataFilter"].update(data_filter)
    if processing:
        data_entry["processing"] = processing

    return {
        "input": {
            "bounds": {
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
                "bbox": bbox,
            },
            "data": [data_entry],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [
                {
                    "identifier": "default",
                    "format": {"type": output_format},
                }
            ],
        },
        "evalscript": evalscript,
    }


def _write_process_output(
    client: SentinelHubClient,
    payload: Dict,
    output_path: Path,
    accept: str,
) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    client.process_image(payload, output_path, accept=accept)
    return str(output_path)


# Downloads reflectance and a validity mask once, then renders the color products locally.
def download_s2_daily_layers(
    client: SentinelHubClient,
    scene: Dict,
    bbox: List[float],
    day_dir: Path,
    config: MultimodalSatelliteConfig,
) -> Dict[str, str]:
    time_from, time_to = acquisition_time_range(scene)
    common_filter = {
        "maxCloudCoverage": config.max_cloud_cover,
        "mosaickingOrder": "leastCC",
    }
    processing = {"upsampling": "BILINEAR", "downsampling": "BILINEAR"}

    payload = process_payload_by_resolution(
        "sentinel-2-l2a", bbox, time_from, time_to, S2_RAW_BUNDLE_EVALSCRIPT,
        config.output_resolution_m, "image/tiff", data_filter=common_filter, processing=processing,
    )
    payload["output"]["responses"].append({"identifier": "data_mask", "format": {"type": "image/tiff"}})
    raw_path, mask_path = download_raw_bundle(client, payload, day_dir, "raw_bands.tif", len(MULTIMODAL_S2_RAW_BANDS))
    return render_s2_layers(raw_path, mask_path, config.include_png_previews)


# Downloads calibrated VV/VH and their mask once, then renders the radar preview locally.
def download_s1_daily_layers(
    client: SentinelHubClient,
    scene: Dict,
    bbox: List[float],
    day_dir: Path,
    config: MultimodalSatelliteConfig,
) -> Dict[str, str]:
    time_from, time_to = acquisition_time_range(scene)
    common_filter = {
        "resolution": "HIGH",
        "acquisitionMode": "IW",
        "polarization": "DV",
        "mosaickingOrder": "mostRecent",
    }
    processing = {
        "orthorectify": True,
        "demInstance": "COPERNICUS_30",
        "backCoeff": "GAMMA0_TERRAIN",
    }
    payload = process_payload_by_resolution(
        "sentinel-1-grd", bbox, time_from, time_to, S1_RAW_BUNDLE_EVALSCRIPT,
        config.output_resolution_m, "image/tiff", data_filter=common_filter, processing=processing,
    )
    payload["output"]["responses"].append({"identifier": "data_mask", "format": {"type": "image/tiff"}})
    raw_path, mask_path = download_raw_bundle(client, payload, day_dir, "vv_vh.tif", 2)
    return render_s1_layers(raw_path, mask_path, config.include_png_previews)


def download_s3_daily_layers(
    client: SentinelHubClient,
    scene: Dict,
    bbox: List[float],
    day_dir: Path,
    config: MultimodalSatelliteConfig,
) -> Dict[str, str]:
    time_from, time_to = acquisition_time_range(scene)
    payload = process_payload_by_resolution(
        "sentinel-3-slstr",
        bbox,
        time_from,
        time_to,
        S3_THERMAL_TIFF_EVALSCRIPT,
        config.s3_resolution_m,
        "image/tiff",
        data_filter={
            "mosaickingOrder": "leastCC",
            "view": "NADIR",
        },
        processing={"upsampling": "NEAREST", "downsampling": "NEAREST"},
    )
    return {
        "thermal_bands_tif": _write_process_output(
            client,
            payload,
            day_dir / "thermal_bands.tif",
            "image/tiff",
        )
    }


# Crops an actual ESA WorldCover map and records its reference year.
def download_land_cover(
    client: SentinelHubClient,
    event: SatelliteEvent,
    bbox: List[float],
    output_root: Path,
    config: MultimodalSatelliteConfig,
) -> Dict:
    width, height = image_dimensions_for_resolution(bbox, config.output_resolution_m)
    return download_worldcover(
        event.start_date, bbox, output_root / "satellite" / "land_cover" / "worldcover.tif",
        width, height,
    )


def empty_sensor_slot(error: Optional[str] = None) -> Dict:
    slot = {
        "available": False,
        "status": "error" if error else "pending",
        "scene": None,
        "outputs": {},
    }
    if error:
        slot["error"] = error
    return slot


def build_quality_summary(days: List[Dict]) -> Dict:
    sensor_names = ["sentinel_2", "sentinel_1", "sentinel_3_slstr"]
    summary = {
        "total_days": len(days),
        "available_days": {},
        "missing_days": {},
        "error_days": {},
    }
    for sensor_name in sensor_names:
        available = sum(1 for day in days if day[sensor_name]["available"])
        summary["available_days"][sensor_name] = available
        summary["missing_days"][sensor_name] = len(days) - available
        summary["error_days"][sensor_name] = sum(
            day[sensor_name].get("status") == "error" for day in days
        )
    return summary


# Checks that a finished slot still has all its downloaded files.
def slot_complete(slot: Optional[Dict]) -> bool:
    slot = slot or {}
    if slot.get("status") in ("no_scene", "no_data", "disabled"):
        return True
    outputs = slot.get("outputs", {})
    return bool(slot.get("available") and outputs and all(
        Path(path).is_file() and Path(path).stat().st_size > 0 for path in outputs.values()
    ))


# Prevents reusing acquisitions produced for another event or configuration.
def manifest_matches(manifest: Dict, event: SatelliteEvent, config: MultimodalSatelliteConfig) -> bool:
    return (manifest.get("schema_version") == MULTIMODAL_SCHEMA_VERSION
            and manifest.get("event") == asdict(event) and manifest.get("config") == asdict(config))


# Marks an event complete only after every requested day and layer is resolved.
def manifest_complete(manifest: Dict) -> bool:
    days = manifest.get("days", [])
    expected = manifest.get("temporal_window", {}).get("days", 0)
    return bool(expected and len(days) == expected and all(
        slot_complete(day.get(sensor)) for day in days
        for sensor in ("sentinel_2", "sentinel_1", "sentinel_3_slstr")
    ) and slot_complete(manifest.get("land_cover")))


# Writes a checkpoint atomically so interrupted runs can resume safely.
def save_manifest(manifest: Dict, path: Path) -> None:
    manifest["quality_summary"] = build_quality_summary(manifest["days"])
    available = manifest["quality_summary"]["available_days"]
    manifest["status"] = (
        "completed" if any(available.values()) else "no_data"
    ) if manifest_complete(manifest) else "partial"
    temporary = path.with_suffix(".json.part")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(path)


# Downloads one sensor-day and distinguishes missing scenes from failed requests.
def fetch_sensor_day(client, bbox, day, sensor, output_root, config):
    selectors = {"sentinel_2": select_daily_s2_scene, "sentinel_1": select_daily_s1_scene,
                 "sentinel_3_slstr": select_daily_s3_scene}
    downloaders = {"sentinel_2": download_s2_daily_layers, "sentinel_1": download_s1_daily_layers,
                   "sentinel_3_slstr": download_s3_daily_layers}
    directories = {"sentinel_2": "sentinel-2", "sentinel_1": "sentinel-1",
                   "sentinel_3_slstr": "sentinel-3-slstr"}
    selector_args = [config.max_cloud_cover] if sensor == "sentinel_2" else []
    scene = selectors[sensor](client, bbox, day, *selector_args)
    if scene is None:
        return {**empty_sensor_slot(), "status": "no_scene"}
    outputs = downloaders[sensor](client, scene, bbox,
                                  output_root / "satellite" / directories[sensor] / day, config)
    slot = {"status": "available", "available": True, "scene": scene_record(scene), "outputs": outputs}
    if sensor in {"sentinel_1", "sentinel_2"}:
        slot["rendering_version"] = RENDERING_VERSION
    return slot


def run_multimodal_satellite_event(
    event: SatelliteEvent,
    config: MultimodalSatelliteConfig,
    output_dir: Optional[str] = None,
    force: bool = False,
) -> Dict:
    if not event.event_id or any(char in event.event_id for char in '/\\:') or event.event_id in (".", ".."):
        raise ValueError("Invalid event id")
    if not (-90 < event.latitude < 90 and -180 <= event.longitude <= 180):
        raise ValueError("Invalid latitude or longitude")
    bbox = event_bbox(event.latitude, event.longitude, config.aoi_half_size_km)
    if bbox[0] < -180 or bbox[2] > 180 or bbox[1] < -90 or bbox[3] > 90:
        raise ValueError("Area crosses the antimeridian or a pole; split-area processing is required")
    output_root = (Path(output_dir or MULTIMODAL_SATELLITE_OUTPUT_DIR) / event.event_id).resolve()
    with satellite_output_lock(output_root):
        return _run_locked_satellite_event(event, config, bbox, output_root, force)


# Reads and updates an event only while its output directory is locked.
def _run_locked_satellite_event(event, config, bbox, output_root, force):
    manifest_path = output_root / "manifest.json"
    dates = analysis_dates(event.start_date, config.window_days)
    daily_records = []
    for day in dates:
        relative_day = (
            datetime.strptime(day, "%Y-%m-%d")
            - datetime.strptime(event.start_date, "%Y-%m-%d")
        ).days
        day_record = {
            "date": day,
            "relative_day": relative_day,
            "sentinel_2": empty_sensor_slot(),
            "sentinel_1": empty_sensor_slot(),
            "sentinel_3_slstr": empty_sensor_slot(),
        }

        if not config.include_sentinel_3:
            day_record["sentinel_3_slstr"]["status"] = "disabled"
        daily_records.append(day_record)

    manifest = {
        "schema_version": MULTIMODAL_SCHEMA_VERSION,
        "event": asdict(event),
        "config": asdict(config),
        "bbox": bbox,
        "output_dimensions": {
            "s1_s2_land_cover": dict(
                zip(
                    ["width", "height"],
                    image_dimensions_for_resolution(bbox, config.output_resolution_m),
                )
            ),
            "sentinel_3_slstr": dict(
                zip(
                    ["width", "height"],
                    image_dimensions_for_resolution(bbox, config.s3_resolution_m),
                )
            ),
        },
        "temporal_window": {
            "from": analysis_dates(event.start_date, config.window_days)[0],
            "to": analysis_dates(event.start_date, config.window_days)[-1],
            "days": 2 * config.window_days + 1,
        },
        "band_sets": {
            "sentinel_2_raw_bands": MULTIMODAL_S2_RAW_BANDS,
            "sentinel_2_false_color_order": ["B12", "B08", "B04"],
            "sentinel_3_thermal_bands": MULTIMODAL_S3_THERMAL_BANDS,
            "sentinel_1_bands": ["VV", "VH"],
        },
        "days": daily_records,
        "land_cover": {**empty_sensor_slot(), "status": "pending" if config.include_land_cover else "disabled"},
        "quality_summary": build_quality_summary(daily_records),
        "notes": {
            "base_layer": (
                "This manifest stores general satellite inputs for a multimodal "
                "dataset. Disaster-specific indices are intentionally not generated."
            ),
            "sentinel_3": (
                "Sentinel-3 SLSTR thermal bands are brightness-temperature source "
                "data, not a downstream LST product."
            ),
            "aoi": "Fixed box around the event coordinates, not the full disaster footprint.",
            "availability": "Available means downloaded, not cloud-free or evidence of disaster damage.",
        },
    }
    if manifest_path.exists() and not force:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest_matches(previous, event, config):
            raise SatelliteConfigurationError("Existing manifest has a different configuration; use a new output directory or --force")
        manifest = previous
    manifest["manifest_path"] = str(manifest_path)
    if manifest_complete(manifest):
        return manifest
    client_id, client_secret = require_copernicus_credentials()
    client = DailyCatalogClient(client_id, client_secret, config.timeout_seconds, dates)
    save_manifest(manifest, manifest_path)
    try:
        for day_record in manifest["days"]:
            day = day_record["date"]
            for sensor in ("sentinel_2", "sentinel_1", "sentinel_3_slstr"):
                if slot_complete(day_record[sensor]):
                    continue
                try:
                    day_record[sensor] = fetch_sensor_day(client, bbox, day, sensor, output_root, config)
                except SentinelHubRequestError as exc:
                    day_record[sensor] = empty_sensor_slot(str(exc))
                    if exc.status_code in (401, 403, 429):
                        raise
                finally:
                    save_manifest(manifest, manifest_path)
            print(f"  {event.event_id} {day}: " + ", ".join(
                f"{sensor}={day_record[sensor]['status']}"
                for sensor in ("sentinel_2", "sentinel_1", "sentinel_3_slstr")
            ), flush=True)
        if not slot_complete(manifest["land_cover"]):
            manifest["land_cover"] = download_land_cover(client, event, bbox, output_root, config)
        save_manifest(manifest, manifest_path)
    finally:
        client.session.close()
    return manifest
