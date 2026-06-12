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
results/multimodal_satellite/<event_id>/
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
      worldcover_lcm10.tif
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

All Sentinel-2 outputs are requested on a 20 m grid. Bands with native 10 m
resolution are downsampled; bands with native 20 m resolution are kept at their
natural scale.

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

Collection:

```text
byoc-828f6b20-8ffd-48f8-a1da-fefd271456db
```

File:

```text
satellite/land_cover/worldcover_lcm10.tif
```

The land-cover layer is saved once per event/AOI, not once per day. It is a
context layer and should be documented as such, especially for events before the
available WorldCover years.

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

## Later Layers

The following products are intentionally postponed:

- flood: MNDWI, NDWI, water-change mask;
- wildfire: NBR, dNBR, burn severity;
- drought/vegetation: NDVI, anomaly metrics;
- any event-specific classifier or threshold.

Those layers should be generated from the saved base data, not mixed into the
first general extraction pass.
