# Multimodal Satellite Dataset Schema

This document describes the general satellite layer for the multimodal disaster
dataset. The goal is to save general, reusable satellite data for every disaster
type before computing disaster-specific indices such as MNDWI, NDVI, NBR, or burn
severity.

## Confirmed Decisions

- Temporal window: fixed `D-10` to `D+10`, for 21 calendar days in total.
- Daily acquisition target: one image per data type per day, when available.
- Bounding box: fixed-size bbox centered on the disaster coordinates.
- Sentinel-2 common resolution: 20 m.
- Sentinel-3 source: Sentinel-3 SLSTR, not a pre-computed downstream LST product.
- Disaster-specific indices: postponed to a later processing layer.

## Event Folder

Each event should be saved under a standard root:

```text
results/multimodal_satellite_2014_plus/<event_id>/
  manifest.json
  satellite/
    sentinel-2/
      YYYY-MM-DD/
        true_color.tif
        false_color.tif
        raw_bands.tif
        true_color_preview.png
        false_color_preview.png
    sentinel-1/
      YYYY-MM-DD/
        vv_vh.tif
        vv_vh_preview.png
    sentinel-3-slstr/
      YYYY-MM-DD/
        thermal_bands.tif
    land_cover/
      worldcover.tif
```

When a scene is not available for a given day and sensor, no data file is written
for that slot. The missing slot is still recorded in `manifest.json`.

## Sentinel-2 L2A

Collection:

```text
sentinel-2-l2a
```

Daily files:

- `true_color.tif`: RGB-like composite using B04, B03, B02.
- `false_color.tif`: false color composite using SWIR, NIR, RED.
- `raw_bands.tif`: multiband TIFF with the requested raw bands.
- PNG previews are generated only for human inspection.

Raw bands saved in `raw_bands.tif`:

```text
B02, B03, B04, B05, B06, B07, B08, B8A, B11, B12
```

All Sentinel-2 outputs use an approximately 20 m grid in EPSG:4326, calculated
from the area dimensions. Bands are resampled with bilinear interpolation;
the output is not a copy of the original native sensor grid.

## Sentinel-1 GRD

Collection:

```text
sentinel-1-grd
```

Daily files:

- `vv_vh.tif`: two-band radar TIFF with VV and VH.
- `vv_vh_preview.png`: dB-scaled RGB preview for quick inspection.

Expected filters:

```text
acquisitionMode = IW
polarization = DV
resolution = HIGH
```

The TIFF stores the two radar channels; the preview is not used as a scientific
layer.

## Sentinel-3 SLSTR

Collection:

```text
sentinel-3-slstr
```

Daily file:

- `thermal_bands.tif`: thermal/temperature-related SLSTR bands.

Initial thermal bands:

```text
S7, S8, S9, F1, F2
```

These bands are kept as general thermal source data for later processing. A
specific LST/burn/drought product can be derived later if needed.

## Land Cover / Land Use

Product: ESA WorldCover, public three-degree COG tiles in EPSG:4326.
Reference maps: 2020 (v100) for events up to 2020, and 2021 (v200) thereafter.
The earlier BYOC LCM10 collection was CLMS, not ESA WorldCover, and is not used.

File:

```text
satellite/land_cover/worldcover.tif
```

The land-cover layer is saved once per event/AOI, not once per day. It is a
context layer and should be documented as such, especially for events before the
available WorldCover years.
The crop uses nearest-neighbor resampling and records product, reference year,
version, source URLs, valid-pixel percentage, CC-BY-4.0 license, and whether the
reference year differs from the event year. Zero is nodata, not a land-cover class.
Source: [ESA WorldCover data access](https://esa-worldcover.org/en/data-access).

## Manifest

`manifest.json` is the authoritative index for an event. It should contain:

- event metadata;
- bbox and temporal window;
- processing configuration;
- one record for every day in the 21-day window;
- availability/missing status for each sensor and day;
- selected scene metadata;
- output file paths;
- cloud-cover metadata where available;
- quality summary counts per sensor.

Schema version: `multimodal-satellite-v2`. Sensor slots distinguish `pending`,
`available`, `no_scene`, `error`, and `disabled`; land cover also uses `no_data`.
The file is checkpointed after every sensor-day. Existing compatible slots are
reused only when their output files still exist and have nonzero size.
Missing-day counts include all unavailable days; error-day counts distinguish
request failures from the absence of an acquisition.

The batch summary includes all disaster types, not just Flood. Its latest row
per event is joined into the final complete dataset with a `satellite_` prefix.
`has_any_satellite_data` counts sensor images, not the static land-cover layer;
availability is not a validation of cloud-free coverage or visible disaster impact.

## Later Layers

The following products are intentionally postponed:

- flood: MNDWI, NDWI, water-change mask;
- wildfire: NBR, dNBR, burn severity;
- drought/vegetation: NDVI, anomaly metrics;
- any event-specific classifier or threshold.

Those layers should be generated from the saved base data, not mixed into the
first general extraction pass.
