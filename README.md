# EnvironmentCausalDataset

## Setup and Execution

This pipeline is designed to be fully automated, modular, and resilient. However, to respect data licensing and keep the repository lightweight, the raw source datasets are excluded from version control.

### Prerequisites & Data Placement

1. Clone this repository to your local machine.
2. Download the original source datasets from your official EMDAT and GDIS providers.
3. Run the pipeline once using the command below; this will automatically generate the required directory tree structure (including the `data/` and `results/` folders).
4. Place your raw CSV files inside the newly created `data/` directory, ensuring they match the expected file names configured in `support/constants.py`.

### Running the Legacy Pipeline

To execute the original dataset compilation, weather data fetching, and checkpointed
news loop, run the legacy entry point from the root directory:

```bash
python src/main.py
```

The explicit equivalent is:

```bash
python src/main.py legacy
```

### Running the Final Release Pipeline

The final release workflow is exposed through the same main entry point, using the
`release` subcommand. It is designed for a fresh clone of the repository: after
placing the raw EM-DAT and GDIS files in `data/` and configuring the required
external credentials, the pipeline can regenerate the final derived data.

By default it only prints the planned commands, so it is safe to inspect before
launching long API or LLM runs:

```bash
python src/main.py release --list-steps
python src/main.py release
```

To run the complete workflow from raw inputs to final outputs:

```bash
python src/main.py release --execute
```

The same entry point can also run one group or one specific step:

```bash
python src/main.py release --execute --steps base
python src/main.py release --execute --steps geocoding
python src/main.py release --execute --steps weather
python src/main.py release --execute --steps reasoning
python src/main.py release --execute --steps satellite
python src/main.py release --execute --steps final
python src/main.py release --execute --steps llm-location llm-nominatim-queries
```

Available groups:

- `base`: merge raw EM-DAT and GDIS into `results/disasters_per_satellite.csv`.
- `geocoding`: build the final audited coordinate CSV from EM-DAT, GADM/ADM2,
  Llama location extraction, Nominatim geocoding, and automatic review rules.
- `weather`: fetch Open-Meteo/NASA POWER weather data for the final geocoded
  events.
- `reasoning`: collect news, generate summaries, validate them, extract causal
  chains, and normalize `type_event` labels.
- `satellite`: select events with valid causal chains by default, across all
  disaster types including Flood, and collect Sentinel-1/2/3 and ESA WorldCover.
- `final`: assemble the all-event CSV and export the selected public JSON/image package.

The full pipeline keeps the detailed outputs for each processing stage and also
produces a final event-level dataset for inspection:

```text
results/final_environmental_causal_dataset_2014_plus.csv
results/final_environmental_causal_dataset_2014_plus_summary.csv
results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2.csv
results/release/data/dataset.json
results/release/images/
results/release/package_manifest.json
```

The complete CSV has one row per event and joins the final position, weather
summary, news coverage, validated event summary, normalized causal chain, and
general satellite availability and manifest paths when available. The separate files remain the detailed
audit/source artifacts for each stage.

The public JSON is a separate intersection: accepted coordinates, valid causal
steps and existing satellite images. It uses the approved compact schema, with
`"none"` for missing data, EM-DAT impacts, weather means, URL-only news references
and causal steps with quotes. Image files are copied into portable category
directories; no models, downloads or uploads are triggered by the exporter.

```bash
python src/export_final_event_dataset.py --dry-run
python src/main.py release --execute --steps release-package --release-limit 1 --release-output-dir results/release_preview
python src/main.py release --execute --steps release-package
```

Inspect the real preview before running the complete export. Keep separate
output directories for previews and full releases; exact interrupted exports
can be resumed. See [the release schema and cluster instructions](docs/final_release_dataset.md)
for selection rules, missing values, provenance limitations and image packaging.

If the release inputs have no compatible weather yet, retrieve it only for the
already exportable coordinate/chain/image intersection:

```bash
python src/fetch_weather_batch.py --release-events-only --dry-run
python src/main.py release --execute --steps weather --weather-release-events-only --weather-sleep-seconds 3
```

This targeted mode requires existing causal chains and satellite manifests and
does not rerun them. The ordinary `all` pipeline still collects weather for all
accepted geocoded events. Weather v2 preserves missing values, source/units and
per-variable daily coverage, resumes successful compatible records, and retries
failures. Use the standalone `--retry-partial` option to retry incomplete series.

Heavy steps have external requirements:

- `gadm-download` downloads GADM files used for administrative fallback.
- `llm-location` and `summary` use `meta-llama/Llama-3.1-70B-Instruct` by default.
- `causal-chain` uses `Qwen/Qwen2.5-72B-Instruct` by default.
- `weather` calls Open-Meteo and falls back to NASA POWER when possible.
- `llm-nominatim-geocode` calls the public Nominatim endpoint with a polite delay.
- `news` calls the configured news sources and should be run with a polite sleep.
- `satellite-general` requires Copernicus/Sentinel Hub credentials and `rasterio`.

### General Satellite Batch

The release workflow does not filter by disaster type unless
`--satellite-disaster-type` is explicitly supplied. Events need an exact start
date and valid coordinates; dates are not invented for incomplete records.
By default, `--satellite-all-events false` also requires a valid, non-empty
causal chain from the normalized causal CSV. Use `true` to collect satellite
data for all events with usable dates and coordinates instead:

```bash
python src/main.py release --steps satellite --satellite-all-events false
python src/main.py release --steps satellite --satellite-all-events true
```

These commands only show the plan; add `--execute` to run it. In the full
workflow, causal-chain extraction and normalization already precede satellite
selection. For a satellite-only run, the default `false` mode requires the
existing normalized causal CSV; override it with `--normalized-causal-csv PATH`.
The `true` mode does not require or read that file for satellite selection.

An eligible chain has at least one saved step, valid JSON and step fields, and
one of these parse statuses: `parsed`, `parsed_with_dropped_items`, or
`parsed_with_dropped_unsupported_quotes`. The last two retain valid steps after
others were discarded. Empty, skipped, malformed or missing chains are excluded;
`causal_chain_length` alone is not used as proof. This selection reuses the
earlier evidence validation and does not call the LLM or repeat quote matching.
It does not establish that the extracted causal links are scientifically correct.

The preparation step writes an input table, a row-level exclusion audit, and
counts by disaster type and selection reason. The audit records
`no_valid_causal_chain` where applicable, plus `causal_chain_status`,
`satellite_all_events` and `causal_chain_source_csv`. Date/coordinate exclusions
take precedence, so each event has one main selection reason.

Standalone preparation and batch commands use `--all-events false` (default)
or `--all-events true`, with `--causal-csv PATH` for a custom causal source.
The batch rechecks the policy before applying `--limit`, including when an old
unfiltered input CSV is used. A missing source, missing required columns or
duplicate causal event ids stops the run before API calls; it never silently
falls back to all events. Explicit event ids must also satisfy the policy unless
`--all-events true` is supplied. The single-event extraction tool remains a
manual tool and is not part of this batch selection policy.

Changing the flag only changes selection: existing downloads are kept, and
compatible selected checkpoints are reused. Previously downloaded events outside
the new subset remain on disk and in the historical summary/final dataset.
No sensor, window or resolution changes. Use the `satellite` group to regenerate
the event table when switching back to `true`; a standalone downloader cannot
restore rows previously removed from its input CSV. Selecting fewer events does
not restore exhausted account quotas or guarantee that the remaining quota is sufficient.

The default area is a fixed approximately 20 km by 20 km box around each event's
coordinates, not the full disaster footprint or an administrative bounding box.
The temporal window is D-10 through D+10, including the event day (21 days).
Up to one acquisition per sensor per day is selected when available:

- Sentinel-2: RGB and B12/B08/B04 false-color GeoTIFFs, a ten-band raw GeoTIFF
  (B02, B03, B04, B05, B06, B07, B08, B8A, B11, B12), plus PNG previews;
  bands are resampled to an approximately 20 m output grid.
- Sentinel-1: VV/VH GeoTIFF and PNG preview, using IW dual-polarization data.
- Sentinel-3 SLSTR: S7, S8, S9, F1, F2 brightness temperatures in kelvin at
  approximately 1 km, **not a derived Land Surface Temperature product**.
- ESA WorldCover: one categorical GeoTIFF per event, cropped from public COGs
  using nearest-neighbor resampling, with reference year and source URLs.
  Available reference maps are 2020 (v100) and 2021 (v200); the nearer reference
  is used even for other event years and the mismatch is explicitly recorded.
  This is not a contemporaneous land-cover observation for every event.

No MNDWI, NDVI, NBR or other disaster-specific indices are generated. Missing
acquisitions are expected, particularly before a sensor's archive begins.
Cloud cover is scene-level metadata; a downloaded image is not necessarily
cloud-free or evidence of damage. Polar and antimeridian-crossing areas require
split-area handling and are reported as errors, never silently shifted.

Outputs are separate from the historical Flood batch:

```text
results/multimodal_satellite_2014_plus/events.csv
results/multimodal_satellite_2014_plus/events_selection_audit.csv
results/multimodal_satellite_2014_plus/events_selection_summary.csv
results/multimodal_satellite_2014_plus/batch_summary.csv
results/multimodal_satellite_2014_plus/<event_id>/manifest.json
results/multimodal_satellite_2014_plus/<event_id>/satellite/<sensor>/<date>/...
```

The manifest records configuration, area, dates, chosen scenes, cloud cover,
files, and per-sensor errors. Each finished sensor-day is checkpointed atomically.
Restarting the same command reuses compatible completed work and retries failed
or missing downloads. A configuration change requires a new output directory or
an explicit `--force` on the standalone batch script. Run only one satellite
batch against a given output directory at a time.

The updated batch holds a `.satellite.lock` in its output root, and each event
has a separate lock around its manifest and product writes. A second updated
batch using the same root, or a second writer for the same event, fails before
starting new downloads. Locking uses [filelock](https://py-filelock.readthedocs.io/en/latest/)
and must be supported by the filesystem; verify it on the cluster's shared storage.
Do not delete lock files to bypass an active writer. Stop older running versions
before deploying this change, since they do not participate in locking.
On the Linux cluster, run the offline tests with temporary fixtures on the
same filesystem as the satellite outputs to check inter-process locking there:

```bash
TMPDIR="$PWD/results/multimodal_satellite_2014_plus" python -m unittest discover -s tests -v
```

The tests create and clean up their own temporary directories, use synthetic
imagery, and make no live satellite requests.

Raw S1/S2 downloads and local composites use private temporary subdirectories
on the destination filesystem, rather than shared `raw_bands.tif.part` names.
Validated files are promoted to their unchanged final names, and cleanup removes
only the current attempt's staging directory. Interrupted downloads remain
resumable; these changes do not invalidate existing compatible manifests.

The batch summary records each attempt, so repeated runs may add rows for the
same event; the final dataset keeps the latest record per event. `completed`
means all requested checks finished and some sensor imagery was downloaded;
`no_data` means the checks finished without sensor imagery; `partial`/`error`
need a retry or intervention. `skipped_existing` means a finished compatible
checkpoint was reused; `manifest_status` preserves its underlying outcome.
Available/missing/error-day counts are distinct, and `has_any_satellite_data`
does not count WorldCover alone as event imagery.

For a specific SLSTR renderer error caused by a missing provider source file
(HTTP 500 wrapping a source-file 404), the downloader tries up to three distinct
same-day acquisitions in the usual quality order. Other sensors and completed
days are reused. Attempts are recorded in the sensor slot's `scene_attempts`;
if none succeeds, it remains an error, not an absent scene or a completed event.
Authentication, quota and generic server errors do not enable this fallback.
The existing HTTP retries still apply separately to each acquisition attempt.

Rate-limit and transient server errors use bounded retries. Authentication,
permission or persistent rate-limit failures stop the batch instead of failing
thousands of subsequent events. A free-disk reserve (5 GiB by default) is checked
before each new event; it is not a guarantee that the full batch will fit.
Raw ten-band S2 output alone is about 40 MB per 1000x1000 acquisition before
compression, so check storage and account quotas before the massive run.

The general module downloads raw bands and an explicit `data_mask.tif` together
in one Process API response per S1/S2 acquisition. S2 true/false-color GeoTIFFs
and PNG previews, and the S1 PNG preview, are rendered locally using numpy,
rasterio, and Pillow. The scientific raw TIFFs retain their ten S2 bands or two
S1 channels. This reduces the default Process calls from five to one for S2,
and from two to one for S1; it does not imply the same percentage reduction in
Processing Units, and it does not replenish an exhausted quota.

The raw request fingerprint is saved in `raw_download.json` after both TIFFs
are validated. A failed local rendering can reuse that download when the
request is unchanged. Complete older event checkpoints are preserved without
requiring an extra mask or regenerating their products. New sensor slots record
`rendering_version=local-from-raw-v1`; no sensor, date window, resolution, or
selection filter is removed by this optimization. PNGs are inspection products,
and display rounding may differ slightly from the former server-side renderer.

After a quota failure, do not use `--force` or delete checkpoints. Get the
account quota restored or extended before a new real smoke test, compare the
measured Requests and Processing Units before/after that test, then reassess
the full batch budget. Offline tests can run without credentials or API calls:

```bash
python -m unittest discover -s tests -v
```

For the existing cluster checkout, after pushing local code changes:

```bash
cd /home/jovyan/users/saverio_polito/Project/EnvironmentCausalDataset
git status --short
git pull --ff-only
source /home/jovyan/users/saverio_polito/venvs/tirocinio/bin/activate
python -m pip install rasterio
python -m pip install filelock
source ~/.copernicus_env
df -h .
python src/main.py release --execute --steps satellite-input
python src/fetch_multimodal_satellite_batch.py --limit 0 --dry-run
```

Stop if the pull fails or the preparation selects no events. Before scaling,
run a real smoke test (the first three eligible events, reusable by the full run):

```bash
python -u src/fetch_multimodal_satellite_batch.py --limit 3 --sleep-seconds 2
```

Inspect its summary and a few TIFF/PNG files. If it finishes without unresolved
errors and storage/quotas permit the run, launch the selected satellite scope and
the final dataset assembly, without rerunning LLM or news stages:

```bash
nohup python -u src/main.py release --execute --steps satellite final \
  > results/multimodal_satellite_2014_plus/batch_massive.log 2>&1 &
echo $!
tail -f results/multimodal_satellite_2014_plus/batch_massive.log
```

`Ctrl+C` stops `tail`, not the background batch. Do not start a second batch while
one is active. If a run stops with incomplete downloads, resolve the reported
cause and repeat the same command. The final assembly runs only after a successful
satellite stage; an explicit `--steps final` can also assemble a partial snapshot.

The older `fetch_satellite_batch.py` and `rank_satellite_events.py` remain
available as standalone, Flood-specific tools; their existing files are untouched.
They are no longer part of the release `all` or `satellite` groups.

Offline regression tests (no account or satellite downloads):

```bash
python -m unittest discover -s tests -v
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

Outputs are written to `results/multimodal_satellite_2014_plus/<event-id>/` and include a
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

To prioritize event-level coverage during the next batches:

```bash
python src/geocode_text_queries.py --dry-run --limit 50 \
  --prioritize-pending-events --one-query-per-pending-event
python src/geocode_text_queries.py --limit 50 \
  --prioritize-pending-events --one-query-per-pending-event
```

To summarize how many events are recovered after text geocoding:

```bash
python src/summarize_text_geocoding_events.py
```

To inspect `matched_review` text geocoding results before accepting them:

```bash
python src/analyze_text_geocoding_review_quality.py
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
