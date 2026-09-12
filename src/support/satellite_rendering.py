import hashlib
import io
import json
import tarfile
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

from support.constants import MULTIMODAL_S2_RAW_BANDS


RENDERING_VERSION = "local-from-raw-v1"


# Checks that the downloaded bands and validity mask share the requested grid.
def validate_raw_pair(raw_path, mask_path, band_count, payload):
    with rasterio.open(raw_path) as raw, rasterio.open(mask_path) as mask:
        if raw.count != band_count or any(dtype != "float32" for dtype in raw.dtypes):
            raise ValueError("Unexpected raw satellite band count or dtype")
        if mask.count != 1 or mask.dtypes != ("uint8",):
            raise ValueError("Unexpected satellite validity mask format")
        if (raw.width, raw.height) != (payload["output"]["width"], payload["output"]["height"]):
            raise ValueError("Raw satellite image has unexpected dimensions")
        if (raw.crs is None or raw.crs.to_epsg() != 4326 or raw.crs != mask.crs or raw.transform != mask.transform
                or raw.shape != mask.shape):
            raise ValueError("Raw satellite bands and mask use different grids")
        if not np.allclose(tuple(raw.bounds), payload["input"]["bounds"]["bbox"], rtol=0, atol=1e-7):
            raise ValueError("Raw satellite image has unexpected bounds")
        raw.read()
        if not np.isin(mask.read(1), [0, 1]).all():
            raise ValueError("Satellite validity mask contains values other than 0 and 1")


# Reuses only complete raw downloads associated with the same Process API request.
def raw_cache_matches(marker, fingerprint, raw_path, mask_path, band_count, payload):
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("request_sha256") != fingerprint:
            return False
        validate_raw_pair(raw_path, mask_path, band_count, payload)
        return True
    except (OSError, ValueError, rasterio.errors.RasterioError):
        return False


# Downloads raw bands and their mask together without extracting arbitrary archive paths.
def download_raw_bundle(client, payload, day_dir, raw_filename, band_count):
    day_dir = Path(day_dir)
    day_dir.mkdir(parents=True, exist_ok=True)
    raw_path = day_dir / raw_filename
    mask_path = day_dir / "data_mask.tif"
    marker = day_dir / "raw_download.json"
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    if raw_cache_matches(marker, fingerprint, raw_path, mask_path, band_count, payload):
        return raw_path, mask_path

    content = client.process_bytes(payload, "application/tar", f"Process request for {raw_filename} and data_mask.tif")
    targets = {"default": raw_path, "data_mask": mask_path}
    temporary = {key: path.with_suffix(path.suffix + ".part") for key, path in targets.items()}
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            seen = set()
            for member in archive:
                key, _, extension = member.name.rpartition(".")
                if not member.isfile() or key not in targets or extension not in {"tif", "tiff"} or key in seen:
                    raise ValueError(f"Unexpected file in satellite response: {member.name}")
                with archive.extractfile(member) as source:
                    temporary[key].write_bytes(source.read())
                seen.add(key)
            if seen != set(targets):
                raise ValueError("Satellite response is missing raw bands or validity mask")
        validate_raw_pair(temporary["default"], temporary["data_mask"], band_count, payload)
        marker.unlink(missing_ok=True)
        for key, path in targets.items():
            temporary[key].replace(path)
        marker_part = marker.with_suffix(".json.part")
        marker_part.write_text(json.dumps({"request_sha256": fingerprint}), encoding="utf-8")
        marker_part.replace(marker)
    finally:
        for path in temporary.values():
            path.unlink(missing_ok=True)
    return raw_path, mask_path


# Reads the scientific bands and mask without modifying the downloaded files.
def read_render_inputs(raw_path, mask_path, band_count):
    with rasterio.open(raw_path) as raw, rasterio.open(mask_path) as mask:
        if (raw.count != band_count or mask.count != 1 or raw.crs is None
                or raw.crs != mask.crs or raw.transform != mask.transform or raw.shape != mask.shape):
            raise ValueError("Cannot render satellite bands with an incompatible validity mask")
        return raw.read(), mask.read(1) == 1, raw.profile.copy()


# Converts display values to integer RGB pixels using the existing clipping and scaling.
def scale_rgb(values, valid, maximum):
    valid = valid & np.isfinite(values).all(axis=0)
    values = np.where(valid[np.newaxis, :, :], values, 0)
    return np.floor(np.clip(values, 0, 1) * maximum + 0.5).astype(
        "uint16" if maximum == 65535 else "uint8"
    )


# Writes a georeferenced color composite while preserving the source grid and mask.
def write_rgb_tiff(path, pixels, valid, profile, raw_path):
    path = Path(path)
    temporary = path.with_suffix(".tif.part")
    try:
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
            with rasterio.open(
                temporary, "w", driver="GTiff", width=profile["width"], height=profile["height"],
                crs=profile["crs"], transform=profile["transform"], count=3,
                dtype="uint16", photometric="RGB", compress="deflate",
            ) as image:
                image.write(pixels)
                image.write_mask(valid.astype("uint8") * 255)
                image.update_tags(derived_from=Path(raw_path).name, rendering_version=RENDERING_VERSION)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


# Writes an RGB preview locally without making another satellite API request.
def write_rgb_png(path, pixels):
    path = Path(path)
    temporary = path.with_suffix(".png.part")
    try:
        Image.fromarray(np.moveaxis(pixels, 0, -1)).save(temporary, format="PNG")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


# Builds Sentinel-2 color composites and previews from the ten reflectance bands.
def render_s2_layers(raw_path, mask_path, include_png_previews=True):
    raw_path = Path(raw_path)
    bands, valid, profile = read_render_inputs(raw_path, mask_path, len(MULTIMODAL_S2_RAW_BANDS))
    outputs = {"raw_bands_tif": str(raw_path), "data_mask_tif": str(mask_path)}
    for name, order in [("true_color", ["B04", "B03", "B02"]), ("false_color", ["B12", "B08", "B04"])]:
        indices = [MULTIMODAL_S2_RAW_BANDS.index(band) for band in order]
        values = bands[indices].astype("float64") * 2.5
        display_valid = valid & np.isfinite(values).all(axis=0)
        outputs[f"{name}_tif"] = write_rgb_tiff(
            raw_path.parent / f"{name}.tif", scale_rgb(values, display_valid, 65535),
            display_valid, profile, raw_path,
        )
        if include_png_previews:
            outputs[f"{name}_preview_png"] = write_rgb_png(
                raw_path.parent / f"{name}_preview.png", scale_rgb(values, display_valid, 255),
            )
    return outputs


# Builds the Sentinel-1 dB preview while leaving the calibrated VV and VH bands unchanged.
def render_s1_layers(raw_path, mask_path, include_png_previews=True):
    raw_path = Path(raw_path)
    outputs = {"vv_vh_tif": str(raw_path), "data_mask_tif": str(mask_path)}
    if include_png_previews:
        bands, valid, _ = read_render_inputs(raw_path, mask_path, 2)
        valid = valid & np.isfinite(bands).all(axis=0)
        values = np.zeros((3, *valid.shape), dtype="float64")
        positive = (bands > 0) & valid[np.newaxis, :, :]
        values[:2][positive] = (10 * np.log10(bands.astype("float64")[positive]) + 25) / 25
        values[2] = valid
        outputs["vv_vh_preview_png"] = write_rgb_png(
            raw_path.parent / "vv_vh_preview.png", scale_rgb(values, valid, 255),
        )
    return outputs
