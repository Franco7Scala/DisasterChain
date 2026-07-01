# EnvironmentCausalDataset

## Setup and Execution

This pipeline is designed to be fully automated, modular, and resilient. However, to respect data licensing and keep the repository lightweight, the raw source datasets are excluded from version control.

### Prerequisites & Data Placement

1. Clone this repository to your local machine.
2. Download the original source datasets from your official EMDAT and GDIS providers.
3. Run the pipeline once using the command below; this will automatically generate the required directory tree structure (including the `data/` and `results/` folders).
4. Place your raw CSV files inside the newly created `data/` directory, ensuring they match the expected file names configured in `support/constants.py`.

### Running the Pipeline

To execute the entire dataset compilation, weather data fetching, and causal aggregation loop, simply run the main script from the root directory:

```bash
python src/main.py
```

### Satellite Event Extraction

Set Copernicus Data Space Sentinel Hub credentials in the environment:

```bash
export COPERNICUS_CLIENT_ID="<client-id>"
export COPERNICUS_CLIENT_SECRET="<client-secret>"
```

Then run a first flood-event extraction from the generated disaster CSV:

```bash
python src/fetch_satellite_event.py --event-id 2018-0040-BRA
```

Outputs are written to `results/satellite/<event-id>/`. When both Sentinel-2
pre/post scenes are locally usable, the run also creates
`s2_water_change_mask.png` for candidate new water after the event. See
`docs/cluster_and_satellite.md` for the cluster/JupyterLab setup and satellite
workflow.

To inspect a small batch before making API calls:

```bash
python src/fetch_satellite_batch.py --dry-run --limit 5
```

To run a controlled batch on recent flood events:

```bash
python src/fetch_satellite_batch.py --limit 3 --image-size 256 --max-cloud-cover 70 --window-days 30
```

The batch runner skips events that already have a manifest unless `--force` is
provided, and appends a compact summary to `results/satellite/batch_summary.csv`.

### Multimodal Satellite Base Layer

The general multimodal satellite extraction is separate from the first flood
mapping workflow. It saves general source layers for the fixed `D-10` to `D+10`
window, without computing disaster-specific indices:

```bash
python src/fetch_multimodal_satellite_event.py --event-id 2018-0040-BRA --dry-run
python src/fetch_multimodal_satellite_event.py --event-id 2018-0040-BRA
```

Outputs are written to `results/multimodal_satellite/<event-id>/` and include a
daily manifest for Sentinel-2, Sentinel-1, Sentinel-3 SLSTR, and land cover when
available. See `docs/multimodal_satellite_schema.md` for the target structure.

To inspect a small multimodal batch before API calls:

```bash
python src/fetch_multimodal_satellite_batch.py --dry-run --limit 3
```

To run a controlled batch:

```bash
python src/fetch_multimodal_satellite_batch.py --limit 2
```

When explicit `--event-id` values are provided, the full requested list is used
unless `--limit` is also set.

To check whether older Landsat collections can be reached before integrating
them into the pipeline:

```bash
python src/probe_landsat_endpoint.py --event-id 2014-0317-USA --collection landsat-ot-l2 --dry-run
python src/probe_landsat_endpoint.py --event-id 2014-0317-USA --collection landsat-ot-l2
```

After the catalog probe succeeds, a small Landsat 8-9 L2 sample can be downloaded
separately from the main pipeline:

```bash
python src/fetch_landsat_sample.py --event-id 2014-0317-USA --collection landsat-ot-l2
```

To inspect recent EM-DAT geocoding coverage and normalize administrative units:

```bash
python src/analyze_recent_emdat_geocoding.py --start-date 2014-04-03
```

Download the needed GADM country files in JSON/GeoJSON format and place them
under `data/gadm/`. Then build prototype bounding boxes from those local GADM
files:

```bash
python src/download_gadm_files.py --unit-level 2 --dry-run --top-countries 10
python src/download_gadm_files.py --unit-level 2 --top-countries 10 --download
python src/build_admin_unit_bboxes.py --dry-run
python src/build_admin_unit_bboxes.py
python src/build_event_bboxes.py --separate-units --unit-level 2 \
  --output-csv results/recent_emdat_geocoding/event_aois_adm2.csv
python src/summarize_geocoding_coverage.py
python src/analyze_unmatched_admin_units.py
python src/prepare_text_geocoding_candidates.py
```

`build_admin_unit_bboxes.py` first tries exact GADM ids and exact names. If that
does not work, it also tries version-insensitive GADM ids and a conservative
same-country/same-level fuzzy name match. The output CSV stores `match_method`,
`match_score`, and the matched GADM unit id/name so these fallback matches can
be reviewed.

The event-level output includes quality flags for very large administrative
unions. For satellite processing AOIs, the preferred output is the separate
adm_2 file, which keeps multiple affected administrative units as separate AOIs.

`prepare_text_geocoding_candidates.py` is the first free-text geocoding step. It
does not call external services: it only prepares event/place candidate rows,
deduplicated geocoding queries, and a summary CSV for later review.

To compare all remaining events with only natural/environmental disaster types:

```bash
python src/prepare_text_geocoding_candidates.py
python src/prepare_text_geocoding_candidates.py --natural-only
```

To geocode a small cached batch from the natural/environmental query list:

```bash
python src/geocode_text_queries.py --dry-run --limit 20
python src/geocode_text_queries.py --limit 50
```

To summarize how many events are recovered after text geocoding:

```bash
python src/summarize_text_geocoding_events.py
```

The geocoder skips queries already stored in the results CSV unless `--force` is
used. With `--force`, existing rows for the selected queries are replaced rather
than duplicated. Results marked as `matched_review` need manual inspection before
being used as final coordinates, for example when a state/province query returns
a very small bounding box.

The text geocoder also builds a few conservative query variants for common
cases seen during validation, including official country-name aliases, island
abbreviations, accents/apostrophes, and selected administrative-region aliases.
When a query clearly mentions multiple places, component-level matches are kept
as `matched_review` because one geocoded point cannot represent all affected
areas automatically.
For noisier free-text locations, the same step also removes small context
phrases and parentheses to create review-only component queries.
