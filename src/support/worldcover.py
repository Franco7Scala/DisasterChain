import math
from pathlib import Path

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


WORLDCOVER_BASE_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"


# Selects the nearest available WorldCover reference year.
def worldcover_year(start_date: str) -> int:
    return 2020 if int(start_date[:4]) <= 2020 else 2021


# Lists the official three-degree WorldCover tiles intersecting the event area.
def worldcover_tile_urls(bbox, year):
    version = "v100" if year == 2020 else "v200"
    west, south, east, north = bbox
    urls = []
    for lat in range(3 * math.floor(south / 3), 3 * math.ceil(north / 3), 3):
        for lon in range(3 * math.floor(west / 3), 3 * math.ceil(east / 3), 3):
            tile = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"
            urls.append(f"{WORLDCOVER_BASE_URL}/{version}/{year}/map/ESA_WorldCover_10m_{year}_{version}_{tile}_Map.tif")
    return urls


# Reads only the requested area of each public COG and preserves class values.
def worldcover_pixels(urls, bbox, width, height):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    transform = from_bounds(*bbox, width, height)
    pixels = np.zeros((height, width), dtype="uint8")
    sources = []
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=retry))
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="60",
                          GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2"):
            for url in urls:
                response = session.head(url, timeout=60)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                with rasterio.open(url) as source:
                    with WarpedVRT(source, crs="EPSG:4326", transform=transform,
                                   width=width, height=height, nodata=0,
                                   resampling=Resampling.nearest) as cropped:
                        values = cropped.read(1)
                        np.copyto(pixels, values, where=values != 0)
                sources.append(url)
    return pixels, transform, sources


# Saves one ESA WorldCover crop and its reference-year provenance per event.
def download_worldcover(start_date, bbox, output_path, width, height):
    import rasterio

    year = worldcover_year(start_date)
    version = "v100" if year == 2020 else "v200"
    record = {
        "product": "ESA WorldCover", "year": year, "version": version,
        "event_year": int(start_date[:4]), "license": "CC-BY-4.0",
        "reference_year_differs_from_event": int(start_date[:4]) != year,
        "available": False, "outputs": {},
    }
    try:
        pixels, transform, sources = worldcover_pixels(
            worldcover_tile_urls(bbox, year), bbox, width, height,
        )
        record["source_urls"] = sources
        record["valid_pixel_percent"] = round(float(np.count_nonzero(pixels) / pixels.size * 100), 4)
        if not np.any(pixels):
            record["status"] = "no_data"
            return record
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(".tif.part")
        with rasterio.open(temporary, "w", driver="GTiff", height=height, width=width,
                           count=1, dtype="uint8", crs="EPSG:4326", transform=transform,
                           nodata=0, compress="deflate") as output:
            output.write(pixels, 1)
            output.set_band_description(1, "ESA WorldCover land-cover class")
            output.update_tags(product="ESA WorldCover", reference_year=year, version=version)
        temporary.replace(output_path)
        record.update(available=True, status="available", outputs={"worldcover_tif": str(output_path)})
    except (OSError, requests.RequestException, rasterio.errors.RasterioError) as exc:
        record.update(status="error", error=str(exc))
    return record
