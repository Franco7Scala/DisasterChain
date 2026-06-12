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
    WORLD_COVER_LCM10_COLLECTION_TYPE,
    WORLD_COVER_LCM10_MAX_YEAR,
    WORLD_COVER_LCM10_MIN_YEAR,
)
from support.satellite_engine import (
    SatelliteEvent,
    SentinelHubClient,
    SentinelHubRequestError,
    event_bbox,
    parse_catalog_datetime,
    require_copernicus_credentials,
)


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


S2_TRUE_COLOR_TIFF_EVALSCRIPT = """
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


S2_FALSE_COLOR_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B04", "B08", "B12", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "UINT16" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  return [scale(sample.B12), scale(sample.B08), scale(sample.B04)];
}

function scale(value) {
  return Math.round(Math.max(0, Math.min(1, value * 2.5)) * 65535);
}
"""


S2_TRUE_COLOR_PREVIEW_EVALSCRIPT = """
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


S2_FALSE_COLOR_PREVIEW_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B04", "B08", "B12", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "AUTO" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  return [2.5 * sample.B12, 2.5 * sample.B08, 2.5 * sample.B04];
}
"""


S2_RAW_BANDS_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: S2_RAW_BANDS,
    output: { id: "default", bands: S2_RAW_BAND_COUNT, sampleType: "FLOAT32" }
  };
}

function evaluatePixel(sample) {
  return [
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
  ];
}
""".replace(
    "S2_RAW_BANDS",
    json.dumps(MULTIMODAL_S2_RAW_BANDS),
).replace(
    "S2_RAW_BAND_COUNT",
    str(len(MULTIMODAL_S2_RAW_BANDS)),
)


S1_VV_VH_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["VV", "VH", "dataMask"],
    output: { id: "default", bands: 2, sampleType: "FLOAT32" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0];
  }
  return [sample.VV, sample.VH];
}
"""


S1_VV_VH_PREVIEW_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["VV", "VH", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "AUTO" }
  };
}

function evaluatePixel(sample) {
  return [toDb(sample.VV), toDb(sample.VH), sample.dataMask];
}

function toDb(linear) {
  if (linear <= 0) {
    return 0;
  }
  var db = 10 * Math.log(linear) / Math.LN10;
  return Math.max(0, Math.min(1, (db + 25) / 25));
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


WORLD_COVER_LCM10_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["LCM10", "dataMask"],
    output: { id: "default", bands: 1, sampleType: "UINT8" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0];
  }
  return [sample.LCM10];
}
"""


def analysis_dates(start_date: str, window_days: int) -> List[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    return [
        (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(-window_days, window_days + 1)
    ]


def day_time_range(day: str) -> Tuple[str, str]:
    return f"{day}T00:00:00Z", f"{day}T23:59:59Z"


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
            float(item.get("properties", {}).get("eo:cloud_cover") or 100.0),
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
            float(item.get("properties", {}).get("eo:cloud_cover") or 100.0),
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


def download_s2_daily_layers(
    client: SentinelHubClient,
    scene: Dict,
    bbox: List[float],
    day_dir: Path,
    config: MultimodalSatelliteConfig,
) -> Dict[str, str]:
    time_from, time_to = day_time_range(scene["properties"]["datetime"][:10])
    common_filter = {
        "maxCloudCoverage": config.max_cloud_cover,
        "mosaickingOrder": "leastCC",
    }
    processing = {"upsampling": "BILINEAR", "downsampling": "BILINEAR"}

    outputs = {
        "true_color_tif": _write_process_output(
            client,
            process_payload_by_resolution(
                "sentinel-2-l2a",
                bbox,
                time_from,
                time_to,
                S2_TRUE_COLOR_TIFF_EVALSCRIPT,
                config.output_resolution_m,
                "image/tiff",
                data_filter=common_filter,
                processing=processing,
            ),
            day_dir / "true_color.tif",
            "image/tiff",
        ),
        "false_color_tif": _write_process_output(
            client,
            process_payload_by_resolution(
                "sentinel-2-l2a",
                bbox,
                time_from,
                time_to,
                S2_FALSE_COLOR_TIFF_EVALSCRIPT,
                config.output_resolution_m,
                "image/tiff",
                data_filter=common_filter,
                processing=processing,
            ),
            day_dir / "false_color.tif",
            "image/tiff",
        ),
        "raw_bands_tif": _write_process_output(
            client,
            process_payload_by_resolution(
                "sentinel-2-l2a",
                bbox,
                time_from,
                time_to,
                S2_RAW_BANDS_TIFF_EVALSCRIPT,
                config.output_resolution_m,
                "image/tiff",
                data_filter=common_filter,
                processing=processing,
            ),
            day_dir / "raw_bands.tif",
            "image/tiff",
        ),
    }

    if config.include_png_previews:
        outputs["true_color_preview_png"] = _write_process_output(
            client,
            process_payload_by_resolution(
                "sentinel-2-l2a",
                bbox,
                time_from,
                time_to,
                S2_TRUE_COLOR_PREVIEW_EVALSCRIPT,
                config.output_resolution_m,
                "image/png",
                data_filter=common_filter,
                processing=processing,
            ),
            day_dir / "true_color_preview.png",
            "image/png",
        )
        outputs["false_color_preview_png"] = _write_process_output(
            client,
            process_payload_by_resolution(
                "sentinel-2-l2a",
                bbox,
                time_from,
                time_to,
                S2_FALSE_COLOR_PREVIEW_EVALSCRIPT,
                config.output_resolution_m,
                "image/png",
                data_filter=common_filter,
                processing=processing,
            ),
            day_dir / "false_color_preview.png",
            "image/png",
        )
    return outputs


def download_s1_daily_layers(
    client: SentinelHubClient,
    scene: Dict,
    bbox: List[float],
    day_dir: Path,
    config: MultimodalSatelliteConfig,
) -> Dict[str, str]:
    time_from, time_to = day_time_range(scene["properties"]["datetime"][:10])
    common_filter = {
        "resolution": "HIGH",
        "acquisitionMode": "IW",
        "polarization": "DV",
        "mosaickingOrder": "mostRecent",
    }
    processing = {
        "orthorectify": "true",
        "demInstance": "COPERNICUS_30",
        "backCoeff": "GAMMA0_TERRAIN",
    }
    outputs = {
        "vv_vh_tif": _write_process_output(
            client,
            process_payload_by_resolution(
                "sentinel-1-grd",
                bbox,
                time_from,
                time_to,
                S1_VV_VH_TIFF_EVALSCRIPT,
                config.output_resolution_m,
                "image/tiff",
                data_filter=common_filter,
                processing=processing,
            ),
            day_dir / "vv_vh.tif",
            "image/tiff",
        )
    }
    if config.include_png_previews:
        outputs["vv_vh_preview_png"] = _write_process_output(
            client,
            process_payload_by_resolution(
                "sentinel-1-grd",
                bbox,
                time_from,
                time_to,
                S1_VV_VH_PREVIEW_EVALSCRIPT,
                config.output_resolution_m,
                "image/png",
                data_filter=common_filter,
                processing=processing,
            ),
            day_dir / "vv_vh_preview.png",
            "image/png",
        )
    return outputs


def download_s3_daily_layers(
    client: SentinelHubClient,
    scene: Dict,
    bbox: List[float],
    day_dir: Path,
    config: MultimodalSatelliteConfig,
) -> Dict[str, str]:
    time_from, time_to = day_time_range(scene["properties"]["datetime"][:10])
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


def land_cover_year_for_event(start_date: str) -> int:
    year = datetime.strptime(start_date, "%Y-%m-%d").year
    return min(max(year, WORLD_COVER_LCM10_MIN_YEAR), WORLD_COVER_LCM10_MAX_YEAR)


def download_land_cover(
    client: SentinelHubClient,
    event: SatelliteEvent,
    bbox: List[float],
    output_root: Path,
    config: MultimodalSatelliteConfig,
) -> Dict:
    year = land_cover_year_for_event(event.start_date)
    output_path = output_root / "satellite" / "land_cover" / "worldcover_lcm10.tif"
    time_from = f"{year}-01-01T00:00:00Z"
    time_to = f"{year}-12-31T23:59:59Z"
    payload = process_payload_by_resolution(
        WORLD_COVER_LCM10_COLLECTION_TYPE,
        bbox,
        time_from,
        time_to,
        WORLD_COVER_LCM10_EVALSCRIPT,
        config.output_resolution_m,
        "image/tiff",
        data_filter={"mosaickingOrder": "mostRecent"},
        processing={"upsampling": "NEAREST", "downsampling": "NEAREST"},
    )
    try:
        path = _write_process_output(client, payload, output_path, "image/tiff")
    except SentinelHubRequestError as exc:
        return {
            "available": False,
            "year": year,
            "outputs": {},
            "error": str(exc),
        }
    return {
        "available": True,
        "year": year,
        "outputs": {"worldcover_lcm10_tif": path},
    }


def empty_sensor_slot(error: Optional[str] = None) -> Dict:
    slot = {
        "available": False,
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
    }
    for sensor_name in sensor_names:
        available = sum(1 for day in days if day[sensor_name]["available"])
        summary["available_days"][sensor_name] = available
        summary["missing_days"][sensor_name] = len(days) - available
    return summary


def run_multimodal_satellite_event(
    event: SatelliteEvent,
    config: MultimodalSatelliteConfig,
    output_dir: Optional[str] = None,
) -> Dict:
    client_id, client_secret = require_copernicus_credentials()
    client = SentinelHubClient(client_id, client_secret, config.timeout_seconds)

    bbox = event_bbox(event.latitude, event.longitude, config.aoi_half_size_km)
    output_root = Path(output_dir or MULTIMODAL_SATELLITE_OUTPUT_DIR) / event.event_id
    output_root.mkdir(parents=True, exist_ok=True)

    daily_records = []
    for day in analysis_dates(event.start_date, config.window_days):
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

        try:
            s2_scene = select_daily_s2_scene(
                client,
                bbox,
                day,
                config.max_cloud_cover,
            )
            if s2_scene:
                s2_outputs = download_s2_daily_layers(
                    client,
                    s2_scene,
                    bbox,
                    output_root / "satellite" / "sentinel-2" / day,
                    config,
                )
                day_record["sentinel_2"] = {
                    "available": True,
                    "scene": scene_record(s2_scene),
                    "outputs": s2_outputs,
                }
        except SentinelHubRequestError as exc:
            day_record["sentinel_2"] = empty_sensor_slot(str(exc))

        try:
            s1_scene = select_daily_s1_scene(client, bbox, day)
            if s1_scene:
                s1_outputs = download_s1_daily_layers(
                    client,
                    s1_scene,
                    bbox,
                    output_root / "satellite" / "sentinel-1" / day,
                    config,
                )
                day_record["sentinel_1"] = {
                    "available": True,
                    "scene": scene_record(s1_scene),
                    "outputs": s1_outputs,
                }
        except SentinelHubRequestError as exc:
            day_record["sentinel_1"] = empty_sensor_slot(str(exc))

        if config.include_sentinel_3:
            try:
                s3_scene = select_daily_s3_scene(client, bbox, day)
                if s3_scene:
                    s3_outputs = download_s3_daily_layers(
                        client,
                        s3_scene,
                        bbox,
                        output_root / "satellite" / "sentinel-3-slstr" / day,
                        config,
                    )
                    day_record["sentinel_3_slstr"] = {
                        "available": True,
                        "scene": scene_record(s3_scene),
                        "outputs": s3_outputs,
                    }
            except SentinelHubRequestError as exc:
                day_record["sentinel_3_slstr"] = empty_sensor_slot(str(exc))

        daily_records.append(day_record)

    land_cover = None
    if config.include_land_cover:
        land_cover = download_land_cover(client, event, bbox, output_root, config)

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
        "land_cover": land_cover,
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
        },
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest
