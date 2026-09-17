"""Validate satellite manifests and plan a portable, category-based image package."""

import math
from dataclasses import dataclass
from pathlib import Path

from support.final_dataset_records import number, window_dates


SENSORS = ("sentinel_2", "sentinel_1", "sentinel_3_slstr")
FINISHED_EMPTY = {"no_scene", "no_data", "disabled"}
PRODUCTS = {
    "sentinel_2": {
        "true_color_tif": ("sentinel_2_rgb", "tif"),
        "true_color_preview_png": ("sentinel_2_rgb", "png"),
        "false_color_tif": ("sentinel_2_false_color", "tif"),
        "false_color_preview_png": ("sentinel_2_false_color", "png"),
        "raw_bands_tif": ("sentinel_2_raw_bands", "tif"),
        "data_mask_tif": ("sentinel_2_data_mask", "tif"),
    },
    "sentinel_1": {
        "vv_vh_tif": ("sentinel_1_sar", "tif"),
        "vv_vh_preview_png": ("sentinel_1_sar", "png"),
        "data_mask_tif": ("sentinel_1_data_mask", "tif"),
    },
    "sentinel_3_slstr": {"thermal_bands_tif": ("sentinel_3_thermal", "tif")},
}


@dataclass(frozen=True)
class Asset:
    source: Path
    destination: str
    size: int


class SatelliteRejected(ValueError):
    pass


# Checks paths and image signatures without importing a downloader or trusting arbitrary manifest paths.
def image_asset(value, event_dir, destination, project_root):
    if not isinstance(value, str) or not value.strip():
        raise SatelliteRejected("missing_asset_path")
    path = Path(value)
    source = (path if path.is_absolute() else project_root / path).resolve()
    if not source.is_relative_to(event_dir.resolve()):
        raise SatelliteRejected("asset_outside_event_directory")
    if not source.is_file() or source.stat().st_size == 0:
        raise SatelliteRejected("missing_or_empty_asset")
    with source.open("rb") as handle:
        header = handle.read(8)
    extension = Path(destination).suffix
    valid = (header[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
             if extension == ".tif" else header == b"\x89PNG\r\n\x1a\n")
    if not valid:
        raise SatelliteRejected("invalid_image_signature")
    return Asset(source, destination, source.stat().st_size)


# Requires the exact event, accepted coordinates and 21-day window used by the final record.
def validate_manifest(manifest, event_id, row, start):
    event = manifest.get("event") or {}
    if event.get("event_id") != event_id or event.get("start_date") != start.isoformat():
        raise SatelliteRejected("manifest_event_or_date_mismatch")
    for field in ("latitude", "longitude"):
        value = number(event.get(field))
        expected = number(row.get(field))
        if value is None or expected is None or not math.isclose(value, expected, rel_tol=0, abs_tol=1e-6):
            raise SatelliteRejected("manifest_coordinate_mismatch")
    dates = window_dates(start)
    window = manifest.get("temporal_window") or {}
    days = manifest.get("days", [])
    if (window.get("days") != 21 or window.get("from") != dates[0] or window.get("to") != dates[-1]
            or len(days) != 21 or any(not isinstance(day, dict) for day in days)
            or sorted(day.get("date", "") for day in days) != dates):
        raise SatelliteRejected("manifest_window_mismatch")
    bbox = manifest.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4 or any(number(v) is None for v in bbox):
        raise SatelliteRejected("invalid_bbox")
    west, south, east, north = map(float, bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90
            and west <= float(row["longitude"]) <= east and south <= float(row["latitude"]) <= north):
        raise SatelliteRejected("invalid_bbox")
    return sorted(days, key=lambda day: day["date"])


# Converts existing acquisitions into portable product paths while retaining each sensor-day's status.
def satellite_fields(manifest, event_id, row, start, event_dir, project_root, *, require_complete=False):
    days = validate_manifest(manifest, event_id, row, start)
    availability = {"day_index": list(range(21)), **{sensor: [] for sensor in SENSORS}}
    categories = {category: [] for products in PRODUCTS.values() for category, _ in products.values()}
    band_sets = manifest.get("band_sets") or {}
    assets = []
    any_image = False
    complete = True
    for index, day in enumerate(days):
        for sensor in SENSORS:
            slot = day.get(sensor) or {}
            status = slot.get("status")
            if status not in FINISHED_EMPTY | {"available", "pending", "error"}:
                raise SatelliteRejected("unknown_sensor_status")
            availability[sensor].append(status)
            if status in FINISHED_EMPTY:
                if slot.get("available") or slot.get("outputs"):
                    raise SatelliteRejected("inconsistent_empty_slot")
                continue
            if status != "available":
                complete = False
                continue
            outputs = slot.get("outputs") or {}
            if slot.get("available") is not True or not outputs:
                raise SatelliteRejected("inconsistent_available_slot")
            scene = slot.get("scene") or {}
            records = {}
            for key, path in outputs.items():
                if key not in PRODUCTS[sensor]:
                    raise SatelliteRejected("unsupported_satellite_product: " + key)
                category, extension = PRODUCTS[sensor][key]
                destination = f"images/{event_id}/{category}/day_{index:02d}_{day['date']}.{extension}"
                asset = image_asset(path, event_dir, destination, project_root)
                assets.append(asset)
                product = records.setdefault(category, {
                    "day_index": index, "date": day["date"],
                    "acquisition_time": scene.get("datetime"),
                })
                product[extension] = "../" + destination
                if sensor == "sentinel_2":
                    product["cloud_cover_percent"] = number(scene.get("cloud_cover"))
                if extension == "tif" and "data_mask" not in category:
                    any_image = True
            for category, product in records.items():
                if "tif" not in product:
                    raise SatelliteRejected("preview_without_scientific_image")
                if category in {"sentinel_2_rgb", "sentinel_2_false_color", "sentinel_1_sar"}:
                    product.setdefault("png", None)
                bands = {"sentinel_2_rgb": ["B04", "B03", "B02"],
                         "sentinel_2_false_color": band_sets.get("sentinel_2_false_color_order"),
                         "sentinel_2_raw_bands": band_sets.get("sentinel_2_raw_bands"),
                         "sentinel_1_sar": band_sets.get("sentinel_1_bands"),
                         "sentinel_3_thermal": band_sets.get("sentinel_3_thermal_bands")}
                if category in bands:
                    product["bands"] = bands[category]
                if category == "sentinel_3_thermal":
                    product.update(quantity="brightness_temperature", unit="K")
                categories[category].append(product)
    land = manifest.get("land_cover") or {}
    land_status = land.get("status")
    worldcover = None
    if land.get("available") is True:
        path = (land.get("outputs") or {}).get("worldcover_tif")
        year = number(land.get("year"), count=True)
        destination = f"images/{event_id}/worldcover/worldcover_{year or 'unknown'}.tif"
        assets.append(image_asset(path, event_dir, destination, project_root))
        worldcover = {"reference_year": year, "product": land.get("product"),
                      "tif": "../" + destination}
    elif land_status not in FINISHED_EMPTY:
        complete = False
    if not any_image:
        raise SatelliteRejected("no_satellite_images")
    if require_complete and not complete:
        raise SatelliteRejected("partial_satellite_collection")
    if len({asset.destination for asset in assets}) != len(assets):
        raise SatelliteRejected("duplicate_asset_destination")
    return {
        "status": "completed" if complete else "partial", "bbox": manifest["bbox"],
        "bbox_crs": "EPSG:4326", "availability": availability,
        **categories, "worldcover": worldcover,
    }, assets
