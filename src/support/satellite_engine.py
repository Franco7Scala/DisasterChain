import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from support.constants import (
    CATALOG_SEARCH_URL,
    COPERNICUS_AUTH_URL,
    PROCESS_URL,
    SATELLITE_OUTPUT_DIR,
)
ISO_FRACTION_RE = re.compile(r"(\.\d{1,6})(?=([+-]\d{2}:\d{2}|$))")


class SentinelHubRequestError(RuntimeError):
    pass


@dataclass
class SatelliteEvent:
    event_id: str
    latitude: float
    longitude: float
    start_date: str
    country: Optional[str] = None
    disaster_type: Optional[str] = None
    location: Optional[str] = None


@dataclass
class SceneSelection:
    collection: str
    period: str
    item_id: str
    datetime: str
    cloud_cover: Optional[float] = None
    local_cloud_cover: Optional[float] = None
    local_nodata_percent: Optional[float] = None
    local_valid_percent: Optional[float] = None
    polarization: Optional[str] = None
    instrument_mode: Optional[str] = None


@dataclass
class SatelliteRunConfig:
    aoi_half_size_km: float = 10.0
    window_days: int = 10
    image_size: int = 768
    max_cloud_cover: float = 30.0
    s2_cloud_eval_size: int = 128
    s2_cloud_candidate_limit: int = 8
    s2_usable_local_cloud_cover: float = 30.0
    s2_water_threshold: float = 0.0
    timeout_seconds: int = 90


def event_bbox(latitude: float, longitude: float, half_size_km: float) -> List[float]:
    """Build a WGS84 bbox around the event point using a simple local approximation."""
    lat_delta = half_size_km / 111.32
    cos_lat = max(math.cos(math.radians(latitude)), 0.01)
    lon_delta = half_size_km / (111.32 * cos_lat)
    return [
        round(longitude - lon_delta, 6),
        round(latitude - lat_delta, 6),
        round(longitude + lon_delta, 6),
        round(latitude + lat_delta, 6),
    ]


def date_window(start_date: str, days_before: int, days_after: int) -> Tuple[str, str]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    return (
        (start - timedelta(days=days_before)).strftime("%Y-%m-%d"),
        (start + timedelta(days=days_after)).strftime("%Y-%m-%d"),
    )


def day_range(acquisition_datetime: str) -> Tuple[str, str]:
    acquired = parse_catalog_datetime(acquisition_datetime)
    day = acquired.strftime("%Y-%m-%d")
    return f"{day}T00:00:00Z", f"{day}T23:59:59Z"


def iso_range(from_date: str, to_date: str) -> str:
    return f"{from_date}T00:00:00Z/{to_date}T23:59:59Z"


def require_copernicus_credentials() -> Tuple[str, str]:
    client_id = os.environ.get("COPERNICUS_CLIENT_ID") or os.environ.get("SH_CLIENT_ID")
    client_secret = (
        os.environ.get("COPERNICUS_CLIENT_SECRET") or os.environ.get("SH_CLIENT_SECRET")
    )
    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing Copernicus credentials. Set COPERNICUS_CLIENT_ID and "
            "COPERNICUS_CLIENT_SECRET before running the satellite pipeline."
        )
    return client_id, client_secret


class SentinelHubClient:
    def __init__(self, client_id: str, client_secret: str, timeout_seconds: int = 90):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self._access_token = None

    def _token(self) -> str:
        if self._access_token:
            return self._access_token

        response = self.session.post(
            COPERNICUS_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        self._access_token = response.json()["access_token"]
        return self._access_token

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": accept,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _raise_api_error(response: requests.Response, context: str) -> None:
        if response.ok:
            return

        body = response.text.strip()
        if len(body) > 1200:
            body = body[:1200] + "..."
        raise SentinelHubRequestError(
            f"{context} failed with HTTP {response.status_code} "
            f"{response.reason}. Response body: {body or '<empty>'}"
        )

    def catalog_search(
        self,
        collection: str,
        bbox: List[float],
        from_date: str,
        to_date: str,
        limit: int = 20,
    ) -> List[Dict]:
        payload = {
            "bbox": bbox,
            "datetime": iso_range(from_date, to_date),
            "collections": [collection],
            "limit": limit,
            "fields": {
                "include": [
                    "id",
                    "bbox",
                    "properties.datetime",
                    "properties.eo:cloud_cover",
                    "properties.s1:polarization",
                    "properties.sar:instrument_mode",
                ]
            },
        }

        response = self.session.post(
            CATALOG_SEARCH_URL,
            json=payload,
            headers=self._headers(accept="application/geo+json, application/json"),
            timeout=self.timeout_seconds,
        )
        self._raise_api_error(response, f"Catalog search for {collection}")
        return response.json().get("features", [])

    def process_image(self, payload: Dict, output_path: Path, accept: str) -> None:
        response = self.session.post(
            PROCESS_URL,
            json=payload,
            headers=self._headers(accept=accept),
            timeout=self.timeout_seconds,
        )
        self._raise_api_error(response, f"Process request for {output_path.name}")
        output_path.write_bytes(response.content)

    def process_bytes(self, payload: Dict, accept: str, context: str) -> bytes:
        response = self.session.post(
            PROCESS_URL,
            json=payload,
            headers=self._headers(accept=accept),
            timeout=self.timeout_seconds,
        )
        self._raise_api_error(response, context)
        return response.content


def cql_eq(property_name: str, value: str) -> Dict:
    return {"op": "eq", "args": [{"property": property_name}, value]}


def cql_lte(property_name: str, value: float) -> Dict:
    return {"op": "lte", "args": [{"property": property_name}, value]}


def cql_and(*filters: Dict) -> Dict:
    return {"op": "and", "args": list(filters)}


def _scene_datetime(item: Dict) -> str:
    return item.get("properties", {}).get("datetime", "")


def parse_catalog_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    def normalize_fraction(match: re.Match) -> str:
        fraction = match.group(1)[1:]
        return "." + fraction[:6].ljust(6, "0")

    text = ISO_FRACTION_RE.sub(normalize_fraction, text)
    return datetime.fromisoformat(text)


def _distance_days(item: Dict, target_date: str) -> int:
    acquired = parse_catalog_datetime(_scene_datetime(item))
    target = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=acquired.tzinfo)
    return abs((acquired - target).days)


def select_s1_scene(
    client: SentinelHubClient,
    bbox: List[float],
    from_date: str,
    to_date: str,
    target_date: str,
    period: str,
) -> Optional[SceneSelection]:
    items = client.catalog_search(
        "sentinel-1-grd",
        bbox,
        from_date,
        to_date,
        limit=100,
    )
    candidates = [
        item
        for item in items
        if item.get("properties", {}).get("sar:instrument_mode") == "IW"
        and item.get("properties", {}).get("s1:polarization") == "DV"
    ]
    if not candidates:
        return None

    selected = sorted(candidates, key=lambda item: _distance_days(item, target_date))[0]
    properties = selected.get("properties", {})
    return SceneSelection(
        collection="sentinel-1-grd",
        period=period,
        item_id=selected.get("id", ""),
        datetime=properties.get("datetime", ""),
        polarization=properties.get("s1:polarization"),
        instrument_mode=properties.get("sar:instrument_mode"),
    )


def select_s2_scene(
    client: SentinelHubClient,
    bbox: List[float],
    from_date: str,
    to_date: str,
    target_date: str,
    period: str,
    max_cloud_cover: float,
    cloud_eval_size: int,
    cloud_candidate_limit: int,
) -> Optional[SceneSelection]:
    items = client.catalog_search(
        "sentinel-2-l2a",
        bbox,
        from_date,
        to_date,
        limit=100,
    )
    candidates = []
    for item in items:
        cloud_cover = item.get("properties", {}).get("eo:cloud_cover")
        if cloud_cover is None or float(cloud_cover) <= max_cloud_cover:
            candidates.append(item)

    if not candidates:
        return None

    def tile_score(item: Dict) -> Tuple[float, int]:
        cloud = item.get("properties", {}).get("eo:cloud_cover")
        return (
            float(cloud) if cloud is not None else 100.0,
            _distance_days(item, target_date),
        )

    ranked_candidates = sorted(candidates, key=tile_score)
    evaluated = []
    for item in ranked_candidates[: max(1, cloud_candidate_limit)]:
        try:
            local_cloud = estimate_s2_local_cloud(
                client,
                item,
                bbox,
                max(16, cloud_eval_size),
            )
            evaluated.append((local_cloud, item))
        except SentinelHubRequestError:
            continue

    if evaluated:
        local_cloud, selected = sorted(
            evaluated,
            key=lambda entry: (
                entry[0]["cloud_percent"],
                _distance_days(entry[1], target_date),
                tile_score(entry[1])[0],
            ),
        )[0]
    else:
        local_cloud = None
        selected = ranked_candidates[0]

    properties = selected.get("properties", {})
    cloud_cover = properties.get("eo:cloud_cover")
    return SceneSelection(
        collection="sentinel-2-l2a",
        period=period,
        item_id=selected.get("id", ""),
        datetime=properties.get("datetime", ""),
        cloud_cover=float(cloud_cover) if cloud_cover is not None else None,
        local_cloud_cover=(
            local_cloud["cloud_percent"] if local_cloud is not None else None
        ),
        local_nodata_percent=(
            local_cloud["nodata_percent"] if local_cloud is not None else None
        ),
        local_valid_percent=(
            local_cloud["valid_percent"] if local_cloud is not None else None
        ),
    )


S1_VV_VH_EVALSCRIPT = """
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


S2_TRUE_COLOR_EVALSCRIPT = """
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


S2_WATER_MASK_EVALSCRIPT_TEMPLATE = """
//VERSION=3
function setup() {
  return {
    input: ["B03", "B08", "B11", "SCL", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "AUTO" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  if (isCloudOrShadow(sample.SCL)) {
    return [1, 1, 1];
  }

  var mndwi = index(sample.B03, sample.B11);
  if (mndwi > WATER_THRESHOLD) {
    return [0.0, 0.25, 1.0];
  }
  return [0.55, 0.55, 0.55];
}

function isCloudOrShadow(scl) {
  return [3, 8, 9, 10, 11].includes(scl);
}
""".replace("WATER_THRESHOLD", "{threshold}")


S2_WATER_INDEX_TIFF_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B03", "B08", "B11", "SCL", "dataMask"],
    output: { id: "default", bands: 4, sampleType: "FLOAT32" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [-9999, -9999, 0];
  }
  var mndwi = index(sample.B03, sample.B11);
  var ndwi = index(sample.B03, sample.B08);
  var cloudMask = isCloudOrShadow(sample.SCL) ? 1 : 0;
  return [mndwi, ndwi, sample.dataMask, cloudMask];
}

function isCloudOrShadow(scl) {
  return [3, 8, 9, 10, 11].includes(scl);
}
"""


S2_LOCAL_CLOUD_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["SCL", "dataMask"],
    output: { id: "default", bands: 3, sampleType: "AUTO" }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0, 0, 0];
  }
  if (isCloudOrShadow(sample.SCL)) {
    return [1, 1, 1];
  }
  return [0.55, 0.55, 0.55];
}

function isCloudOrShadow(scl) {
  return [3, 8, 9, 10, 11].includes(scl);
}
"""


def process_payload(
    collection: str,
    bbox: List[float],
    time_from: str,
    time_to: str,
    evalscript: str,
    image_size: int,
    output_format: str,
    data_filter: Optional[Dict] = None,
    processing: Optional[Dict] = None,
) -> Dict:
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
            "width": image_size,
            "height": image_size,
            "responses": [
                {
                    "identifier": "default",
                    "format": {"type": output_format},
                }
            ],
        },
        "evalscript": evalscript,
    }


def estimate_s2_local_cloud(
    client: SentinelHubClient,
    scene_item: Dict,
    bbox: List[float],
    image_size: int,
) -> Dict[str, float]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow and numpy are required to estimate Sentinel-2 local cloud cover."
        ) from exc

    scene_datetime = _scene_datetime(scene_item)
    time_from, time_to = day_range(scene_datetime)
    payload = process_payload(
        "sentinel-2-l2a",
        bbox,
        time_from,
        time_to,
        S2_LOCAL_CLOUD_EVALSCRIPT,
        image_size,
        "image/png",
        data_filter={
            "maxCloudCoverage": 100,
            "mosaickingOrder": "leastCC",
        },
        processing={"upsampling": "NEAREST", "downsampling": "NEAREST"},
    )
    content = client.process_bytes(
        payload,
        accept="image/png",
        context=f"Local cloud evaluation for {scene_item.get('id', 'unknown')}",
    )
    pixels = np.asarray(Image.open(BytesIO(content)).convert("RGB"))
    red = pixels[:, :, 0]
    green = pixels[:, :, 1]
    blue = pixels[:, :, 2]

    cloud = (red > 240) & (green > 240) & (blue > 240)
    nodata = (red < 5) & (green < 5) & (blue < 5)
    valid = ~nodata

    total_pixels = pixels.shape[0] * pixels.shape[1]
    valid_pixels = int(valid.sum())
    cloud_pixels = int((cloud & valid).sum())
    nodata_pixels = int(nodata.sum())

    return {
        "cloud_percent": (
            round((cloud_pixels / valid_pixels) * 100, 4) if valid_pixels else 100.0
        ),
        "nodata_percent": round((nodata_pixels / total_pixels) * 100, 4),
        "valid_percent": round((valid_pixels / total_pixels) * 100, 4),
    }


def download_s1_scene(
    client: SentinelHubClient,
    scene: SceneSelection,
    bbox: List[float],
    output_path: Path,
    image_size: int,
) -> None:
    time_from, time_to = day_range(scene.datetime)
    payload = process_payload(
        "sentinel-1-grd",
        bbox,
        time_from,
        time_to,
        S1_VV_VH_EVALSCRIPT,
        image_size,
        "image/png",
        data_filter={
            "resolution": "HIGH",
            "acquisitionMode": "IW",
            "polarization": "DV",
            "mosaickingOrder": "mostRecent",
        },
        processing={
            "orthorectify": "true",
            "demInstance": "COPERNICUS_30",
            "backCoeff": "GAMMA0_TERRAIN",
        },
    )
    client.process_image(payload, output_path, accept="image/png")


def download_s2_scene(
    client: SentinelHubClient,
    scene: SceneSelection,
    bbox: List[float],
    output_prefix: Path,
    image_size: int,
    max_cloud_cover: float,
    water_threshold: float,
) -> Dict[str, str]:
    time_from, time_to = day_range(scene.datetime)
    common_filter = {
        "maxCloudCoverage": max_cloud_cover,
        "mosaickingOrder": "leastCC",
    }

    true_color_path = output_prefix.with_name(f"{output_prefix.name}_true_color.png")
    true_color_payload = process_payload(
        "sentinel-2-l2a",
        bbox,
        time_from,
        time_to,
        S2_TRUE_COLOR_EVALSCRIPT,
        image_size,
        "image/png",
        data_filter=common_filter,
        processing={"upsampling": "BILINEAR", "downsampling": "BILINEAR"},
    )
    client.process_image(true_color_payload, true_color_path, accept="image/png")

    water_mask_path = output_prefix.with_name(f"{output_prefix.name}_mndwi_mask.png")
    water_evalscript = S2_WATER_MASK_EVALSCRIPT_TEMPLATE.replace(
        "{threshold}", str(water_threshold)
    )
    water_mask_payload = process_payload(
        "sentinel-2-l2a",
        bbox,
        time_from,
        time_to,
        water_evalscript,
        image_size,
        "image/png",
        data_filter=common_filter,
        processing={"upsampling": "BILINEAR", "downsampling": "BILINEAR"},
    )
    client.process_image(water_mask_payload, water_mask_path, accept="image/png")

    water_index_path = output_prefix.with_name(f"{output_prefix.name}_water_indices.tiff")
    water_index_payload = process_payload(
        "sentinel-2-l2a",
        bbox,
        time_from,
        time_to,
        S2_WATER_INDEX_TIFF_EVALSCRIPT,
        image_size,
        "image/tiff",
        data_filter=common_filter,
        processing={"upsampling": "BILINEAR", "downsampling": "BILINEAR"},
    )
    client.process_image(water_index_payload, water_index_path, accept="image/tiff")

    return {
        "true_color_png": str(true_color_path),
        "mndwi_mask_png": str(water_mask_path),
        "water_indices_tiff": str(water_index_path),
    }


def build_s1_change_mask(pre_path: Path, post_path: Path, output_path: Path) -> None:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow and numpy are required to build the Sentinel-1 change mask."
        ) from exc

    pre = np.asarray(Image.open(pre_path).convert("RGB"), dtype=np.int16)
    post = np.asarray(Image.open(post_path).convert("RGB"), dtype=np.int16)

    pre_vv = pre[:, :, 0]
    post_vv = post[:, :, 0]
    post_mask = post[:, :, 2] > 0

    darkening = pre_vv - post_vv
    flood_like = (darkening > 35) & (post_vv < 85) & post_mask

    mask = np.zeros((post.shape[0], post.shape[1], 3), dtype=np.uint8)
    mask[:, :, :] = [40, 40, 40]
    mask[flood_like] = [0, 90, 255]
    Image.fromarray(mask).save(output_path)


def bbox_area_km2(bbox: List[float]) -> float:
    west, south, east, north = bbox
    avg_latitude = (south + north) / 2
    width_km = (
        abs(east - west)
        * 111.32
        * max(math.cos(math.radians(avg_latitude)), 0.01)
    )
    height_km = abs(north - south) * 111.32
    return width_km * height_km


def _class_stats(count: int, total: int, pixel_area_km2: float) -> Dict:
    return {
        "pixels": int(count),
        "percent": round((count / total) * 100, 4) if total else 0.0,
        "area_km2": round(count * pixel_area_km2, 4),
    }


def compute_mask_statistics(mask_path: Path, bbox: List[float], candidate_label: str) -> Dict:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow and numpy are required to compute satellite mask statistics."
        ) from exc

    image = Image.open(mask_path).convert("RGB")
    pixels = np.asarray(image)
    height, width, _ = pixels.shape
    total = width * height
    pixel_area_km2 = bbox_area_km2(bbox) / total if total else 0.0

    red = pixels[:, :, 0]
    green = pixels[:, :, 1]
    blue = pixels[:, :, 2]

    candidate = (blue > 200) & (red < 30) & (green < 140)
    cloud_or_shadow = (red > 240) & (green > 240) & (blue > 240)
    nodata = (red < 5) & (green < 5) & (blue < 5)
    other = ~(candidate | cloud_or_shadow | nodata)

    candidate_count = int(candidate.sum())
    cloud_count = int(cloud_or_shadow.sum())
    nodata_count = int(nodata.sum())
    other_count = int(other.sum())

    return {
        "source": str(mask_path),
        "width": width,
        "height": height,
        "total_pixels": total,
        "bbox_area_km2": round(bbox_area_km2(bbox), 4),
        "pixel_area_km2": round(pixel_area_km2, 8),
        "classes": {
            candidate_label: _class_stats(candidate_count, total, pixel_area_km2),
            "cloud_or_shadow": _class_stats(cloud_count, total, pixel_area_km2),
            "nodata": _class_stats(nodata_count, total, pixel_area_km2),
            "other": _class_stats(other_count, total, pixel_area_km2),
        },
    }


def compute_output_statistics(outputs: Dict[str, str], bbox: List[float]) -> Dict[str, Dict]:
    statistics = {}
    for output_name, output_path in outputs.items():
        if output_name == "s1_change_mask_png":
            statistics[output_name] = compute_mask_statistics(
                Path(output_path),
                bbox,
                "candidate_radar_change",
            )
        elif output_name.endswith("mndwi_mask_png"):
            statistics[output_name] = compute_mask_statistics(
                Path(output_path),
                bbox,
                "candidate_water",
            )
    return statistics


def _s2_scene_usable(
    scene: Optional[SceneSelection],
    usable_local_cloud_cover: float,
) -> bool:
    if scene is None:
        return False
    if scene.local_cloud_cover is None:
        return False
    return scene.local_cloud_cover <= usable_local_cloud_cover


def build_quality_assessment(
    s1_pre: Optional[SceneSelection],
    s1_post: Optional[SceneSelection],
    s2_pre: Optional[SceneSelection],
    s2_post: Optional[SceneSelection],
    outputs: Dict[str, str],
    config: SatelliteRunConfig,
) -> Dict:
    s1_change_available = (
        s1_pre is not None
        and s1_post is not None
        and "s1_change_mask_png" in outputs
    )
    s2_pre_usable = _s2_scene_usable(
        s2_pre,
        config.s2_usable_local_cloud_cover,
    )
    s2_post_usable = _s2_scene_usable(
        s2_post,
        config.s2_usable_local_cloud_cover,
    )
    s2_change_detection_usable = s2_pre_usable and s2_post_usable

    if s2_change_detection_usable and s1_change_available:
        recommended = "sentinel-1-and-sentinel-2"
    elif s1_change_available:
        recommended = "sentinel-1"
    elif s2_post_usable:
        recommended = "sentinel-2-post-event"
    else:
        recommended = "manual-review"

    return {
        "thresholds": {
            "s2_usable_local_cloud_cover": config.s2_usable_local_cloud_cover,
        },
        "s1_change_detection_available": s1_change_available,
        "s2_pre_usable": s2_pre_usable,
        "s2_post_usable": s2_post_usable,
        "s2_change_detection_usable": s2_change_detection_usable,
        "recommended_primary_layer": recommended,
        "reasons": {
            "s2_pre": (
                "usable"
                if s2_pre_usable
                else "missing or local cloud cover above threshold"
            ),
            "s2_post": (
                "usable"
                if s2_post_usable
                else "missing or local cloud cover above threshold"
            ),
            "s1": (
                "pre/post change mask available"
                if s1_change_available
                else "missing pre/post radar pair or change mask"
            ),
        },
    }


def _scene_asdict(scene: Optional[SceneSelection]) -> Optional[Dict]:
    return asdict(scene) if scene else None


def run_satellite_event(
    event: SatelliteEvent,
    config: SatelliteRunConfig,
    output_dir: Optional[str] = None,
) -> Dict:
    client_id, client_secret = require_copernicus_credentials()
    client = SentinelHubClient(client_id, client_secret, config.timeout_seconds)

    bbox = event_bbox(event.latitude, event.longitude, config.aoi_half_size_km)
    output_root = Path(output_dir or SATELLITE_OUTPUT_DIR) / event.event_id
    output_root.mkdir(parents=True, exist_ok=True)

    pre_from, pre_to = date_window(event.start_date, config.window_days, -1)
    post_from, post_to = date_window(event.start_date, 0, config.window_days)

    s1_pre = select_s1_scene(client, bbox, pre_from, pre_to, event.start_date, "pre")
    s1_post = select_s1_scene(client, bbox, post_from, post_to, event.start_date, "post")
    s2_pre = select_s2_scene(
        client,
        bbox,
        pre_from,
        pre_to,
        event.start_date,
        "pre",
        config.max_cloud_cover,
        config.s2_cloud_eval_size,
        config.s2_cloud_candidate_limit,
    )
    s2_post = select_s2_scene(
        client,
        bbox,
        post_from,
        post_to,
        event.start_date,
        "post",
        config.max_cloud_cover,
        config.s2_cloud_eval_size,
        config.s2_cloud_candidate_limit,
    )

    outputs: Dict[str, str] = {}

    if s1_pre and s1_post:
        s1_pre_path = output_root / "s1_pre_vv_vh.png"
        s1_post_path = output_root / "s1_post_vv_vh.png"
        download_s1_scene(client, s1_pre, bbox, s1_pre_path, config.image_size)
        download_s1_scene(client, s1_post, bbox, s1_post_path, config.image_size)
        outputs["s1_pre_vv_vh_png"] = str(s1_pre_path)
        outputs["s1_post_vv_vh_png"] = str(s1_post_path)

        s1_change_path = output_root / "s1_change_mask.png"
        build_s1_change_mask(s1_pre_path, s1_post_path, s1_change_path)
        outputs["s1_change_mask_png"] = str(s1_change_path)

    if s2_pre:
        outputs.update(
            {
                f"s2_pre_{key}": value
                for key, value in download_s2_scene(
                    client,
                    s2_pre,
                    bbox,
                    output_root / "s2_pre",
                    config.image_size,
                    config.max_cloud_cover,
                    config.s2_water_threshold,
                ).items()
            }
        )

    if s2_post:
        outputs.update(
            {
                f"s2_post_{key}": value
                for key, value in download_s2_scene(
                    client,
                    s2_post,
                    bbox,
                    output_root / "s2_post",
                    config.image_size,
                    config.max_cloud_cover,
                    config.s2_water_threshold,
                ).items()
            }
        )

    quality = build_quality_assessment(
        s1_pre,
        s1_post,
        s2_pre,
        s2_post,
        outputs,
        config,
    )

    manifest = {
        "event": asdict(event),
        "config": asdict(config),
        "bbox": bbox,
        "windows": {
            "pre": {"from": pre_from, "to": pre_to},
            "post": {"from": post_from, "to": post_to},
        },
        "scenes": {
            "s1_pre": _scene_asdict(s1_pre),
            "s1_post": _scene_asdict(s1_post),
            "s2_pre": _scene_asdict(s2_pre),
            "s2_post": _scene_asdict(s2_post),
        },
        "outputs": outputs,
        "statistics": compute_output_statistics(outputs, bbox),
        "quality": quality,
        "notes": {
            "s1_change_mask": (
                "Blue pixels are candidate radar darkening areas from VV backscatter. "
                "This is a first-pass mask, not a validated flood product."
            ),
            "s2_mndwi_mask": (
                "Blue pixels are MNDWI values above the configured threshold. "
                "White pixels are clouds, cloud shadows, cirrus, or snow/ice "
                "from the Sentinel-2 SCL band."
            ),
        },
    }

    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def summarize_selected_scenes(scenes: Iterable[Optional[SceneSelection]]) -> List[str]:
    lines = []
    for scene in scenes:
        if scene is None:
            lines.append("- missing scene")
            continue
        label = f"{scene.collection} {scene.period}: {scene.datetime}"
        if scene.cloud_cover is not None:
            label += f" cloud={scene.cloud_cover:.1f}%"
        if scene.local_cloud_cover is not None:
            label += f" local_cloud={scene.local_cloud_cover:.1f}%"
        if scene.polarization:
            label += f" pol={scene.polarization}"
        lines.append(f"- {label}")
    return lines
