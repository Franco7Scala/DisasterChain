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
- Event selection: valid non-empty causal chains and usable coordinates by default;
  `--satellite-all-events true` in the release runner allows all geocoded events.

## Batch Selection

The release option `--satellite-all-events` accepts explicit `true` or `false`
and defaults to `false`. Preparation and the standalone batch expose the same
choice as `--all-events`. Both read the normalized causal CSV in restricted mode
and match event identifiers, not row order. A custom source can be selected with
`--normalized-causal-csv` in the release runner or `--causal-csv` in either script.

Eligibility requires a non-empty JSON chain with numbered steps containing
`type_event`, `description` and `supporting_quote`, and a status of `parsed`,
`parsed_with_dropped_items` or `parsed_with_dropped_unsupported_quotes`. Steps
retained after validation still qualify, even if other steps were dropped.
This does not rerun evidence validation or imply independent scientific validation.
Missing, empty or malformed chains do not qualify. Missing source files or
ambiguous schemas/identifiers stop the run instead of enabling all events.

The selection audit keeps one main exclusion reason per input event, including
`no_valid_causal_chain`, along with `causal_chain_status`, `satellite_all_events`
and `causal_chain_source_csv`. Coordinate/date failures take precedence over
causal exclusions. The true mode does not read the causal file and records
`causal_chain_status=not_checked`.

Selection is separate from the per-event acquisition configuration: changing
scope preserves compatible manifests and does not invalidate downloaded TIFFs.
Only newly selected, incomplete events need further downloads. Previously
collected events outside the subset stay in the historical summary and may
still appear in the final joined dataset; they are not deleted or relabeled.
The prepared input/audit describe the current target, while `batch_summary.csv`
describes all attempts made so far. The complete dataset still keeps all base
events, including those not selected for satellite collection.

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
        data_mask.tif
        raw_download.json
        true_color_preview.png
        false_color_preview.png
    sentinel-1/
      YYYY-MM-DD/
        vv_vh.tif
        data_mask.tif
        raw_download.json
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
- `data_mask.tif`: the provider's validity mask (1 = valid, 0 = no data), not a cloud or disaster mask.
- `raw_download.json`: request fingerprint used to resume local rendering without reprocessing the bands.

Raw bands saved in `raw_bands.tif`:

```text
B02, B03, B04, B05, B06, B07, B08, B8A, B11, B12
```

All Sentinel-2 outputs use an approximately 20 m grid in EPSG:4326, calculated
from the area dimensions. Bands are resampled with bilinear interpolation;
the output is not a copy of the original native sensor grid.

New acquisitions download the ten FLOAT32 reflectance bands and a separate
UINT8 validity mask in a single Process API TAR response. RGB GeoTIFFs are
derived locally with the existing gain of 2.5, clipping to [0, 1], and UINT16
scaling; previews use the same gain with UINT8 scaling. The mask is preserved
in the GeoTIFFs, and their transform and CRS are copied from the raw bands.
The raw reflectance TIFF is not rewritten by the renderer. Rounding at display
quantization boundaries may differ slightly from the older server-side renderer.

## Sentinel-1 GRD

Collection:

```text
sentinel-1-grd
```

Daily files:

- `vv_vh.tif`: two-band radar TIFF with VV and VH.
- `vv_vh_preview.png`: dB-scaled RGB preview for quick inspection.
- `data_mask.tif`: the provider's validity mask, downloaded together with VV/VH.
- `raw_download.json`: request fingerprint for resumable local rendering.

Expected filters:

```text
acquisitionMode = IW
polarization = DV
resolution = HIGH
```

The TIFF stores the two radar channels; the preview is not used as a scientific
layer.

VV/VH remain FLOAT32, orthorectified with the Copernicus 30 m DEM and calibrated
to GAMMA0_TERRAIN by the service. Only the preview is produced locally: VV and
VH use the existing -25 to 0 dB display range, with validity in the blue channel.
Non-positive radar values are displayed as zero; the scientific TIFF is unchanged.

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

The primary acquisition still uses cloud cover and then acquisition time for
ranking. If Process returns HTTP 500 with `RENDERER_EXCEPTION` and an explicit
404 for a `creo://eodata/Sentinel-3/SLSTR/` source file, the downloader tries the
next acquisition on the same calendar day. It considers at most three distinct,
non-overlapping acquisition windows in total; the existing HTTP retries still
apply within each attempt. Changing only a catalog ID at the same timestamp
would reuse the same Process time filter, so those candidates are deduplicated.
The bbox, bands, resolution, NADIR view and 21-day window do not change.
The Process request remains time-filtered, not pinned to a product ID; see the
[SLSTR filtering documentation](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/S3SLSTR.html#filtering-options).

New SLSTR slots record `scene_attempts` for the latest sensor-day run: catalog
scene metadata, outcome, and, on failure, the error, HTTP status and
`missing_source_file` flag. `scene` on a successful slot describes the successful
candidate. If all attempted candidates fail, the slot stays `error` and the
event stays `partial`, not `no_scene` or `completed`. Other errors do not trigger
alternative acquisitions; authentication and quota errors still stop the run.
Completed old slots remain compatible and are reused without new requests.

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

New S1/S2 slots include `rendering_version: local-from-raw-v1`; older completed
slots remain compatible and are not regenerated just to add a validity mask.
The raw cache validates band counts, dtypes, dimensions and mask alignment, and
is reused only for the same request fingerprint. A changed acquisition or
missing/corrupt raw file requires a new download. Response archives are read
only for their expected TIFF members; arbitrary paths are never extracted.

Raw bundles and derived RGB/PNG outputs are staged in unique temporary
subdirectories alongside their destinations. Cleanup never removes another
attempt's `.part` file. Final product names and request fingerprints are unchanged.
The batch output root and each event directory use independent `.satellite.lock`
files via `filelock`, preventing overlapping updated batch writers or concurrent
updates of one event. Locks are separate from the scientific manifest and do
not count as data products. Lock-file presence alone is not a completion or
running-status indicator. Do not manually delete locks held by another process;
old code must be stopped before rollout, and shared-filesystem locking semantics
must be verified on the execution host.

This reduces Process API calls per available acquisition from five to one for
S2 and two to one for S1 with default previews enabled. Catalog lookups, S3,
WorldCover and the 21-day window are unchanged. These call counts are not a PU
budget estimate; account-level quotas still apply and may stop a massive run.

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
