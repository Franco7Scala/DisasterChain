# DisasterChain: a multimodal global disaster dataset linking Earth observation, meteorology and structured narratives

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Version](https://img.shields.io/badge/pypi-1.0.0-orange.svg)](https://pypi.org/project/floppy-tracker/)

Build a multimodal disaster dataset from raw EM-DAT and GDIS files using a single
entry point: `python src/main.py release`. The pipeline merges the source data,
resolves coordinates, retrieves weather and news, generates summaries and causal
chains, downloads satellite products, and creates a portable JSON/image package.
You do not need precomputed geocoding files or the authors' generated results.

## Guide

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Input files](#input-files)
4. [Credentials and service configuration](#credentials-and-service-configuration)
5. [Checks before running](#checks-before-running)
6. [Run the complete pipeline](#run-the-complete-pipeline)
7. [Run individual modules](#run-individual-modules)
8. [Selection and resource controls](#selection-and-resource-controls)
9. [Resume and troubleshoot](#resume-and-troubleshoot)
10. [Outputs and publication](#outputs-and-publication)

## Requirements

Prepare these before starting the full pipeline:

| Requirement | What is needed |
| --- | --- |
| Software | Git, a 64-bit Python environment, and all packages in `requirements.txt`. Use a Python version supported by your chosen PyTorch/Transformers/bitsandbytes combination. |
| Source files | The event-level EM-DAT Excel table and the GDIS disaster-location CSV, with the columns described below. |
| GPU and RAM | A GPU workstation or cluster allocation sized for local 70B/72B inference. CPU-only processing is not the intended configuration for the full run. |
| Storage | Space for both model caches, administrative data, source imagery, intermediate outputs, and a separate copy of selected imagery in the final package. Plan for hundreds of GB, potentially more depending on event count and coverage. |
| Network | Access to Hugging Face, GADM, geocoding, weather, news and Copernicus services. |
| Accounts and quotas | Hugging Face model access, Copernicus OAuth credentials with sufficient quota, a ReliefWeb appname, and an appropriate Nominatim service configuration. |

The default models are `meta-llama/Llama-3.1-70B-Instruct` for location cleaning
and summaries, and `Qwen/Qwen2.5-72B-Instruct` for causal chains. They run locally,
not through an LLM API. The main uses **4-bit quantization** by default.

These models require substantial GPU memory even when quantized. Allow space
for inference buffers and the news context in addition to model weights; do not
assume a GPU fits the model simply because CUDA is available. There is no single
validated minimum VRAM/RAM figure for every configuration. A smaller model is an
explicit change to the experiment and requires output validation.

Downloads and API limits can make the full run lengthy. A free Copernicus quota
is not guaranteed to cover it. The main does not provision hardware, buy credits,
or wait automatically for a quota renewal. On a cluster, obtain a GPU allocation
before the LLM stages and follow the site's scheduler rules.

## Installation

Clone the repository:

```bash
git clone https://github.com/Franco7Scala/EnvironmentCausalDataset.git
cd EnvironmentCausalDataset
```

Create and activate an environment. On Linux, including a cluster:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
mkdir -p data results
```

On Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
New-Item -ItemType Directory -Force data, results | Out-Null
```

Install the CUDA-enabled PyTorch build appropriate to your GPU driver using the
[official PyTorch installation selector](https://pytorch.org/get-started/locally/).
Run its command in this environment, then install the project dependencies:

```bash
python -m pip install -r requirements.txt
python -m pip check
```

The dependency file includes:

| Purpose | Packages |
| --- | --- |
| Tables and Excel | `pandas`, `openpyxl` |
| HTTP, HTML and document extraction | `requests`, `beautifulsoup4`, `lxml`, `pypdf` |
| Local LLM inference | `torch`, `transformers`, `accelerate`, `bitsandbytes` |
| Country names and codes | `pycountry` |
| Arrays, rasters and image previews | `numpy`, `rasterio`, `Pillow` |
| Download/process locking | `filelock` |
| Notebook integration | `ipykernel` |

The commands below assume the repository root is the working directory and this
environment is active. Child scripts use the same Python interpreter as the
main unless `--python` is supplied. Bash-specific commands are labeled; Python
commands on a single line also work in PowerShell. Check GPU/backend support
before using native Windows for quantized inference.

## Input Files

Download the event-level Excel table from the [EM-DAT portal](https://public.emdat.be/)
using the [official access instructions](https://doc.emdat.be/docs/data-accessibility/),
and the disaster-location CSV from the [GDIS dataset](https://data.nasa.gov/dataset/geocoded-disasters-gdis-dataset).
Complete the providers' registration requirements and retain their licenses and
citations. Do not use country/year aggregate tables in place of event records.

The default paths are:

```text
data/
  public_emdat_dal_2000.xlsx
  pend-gdis-1960-2018-disasterlocations.csv
```

EM-DAT must be an **Excel file**, GDIS a **comma-separated CSV**. The first Excel
worksheet must contain the table, with headers in the first row. Preserve
original column names, capitalization and units; do not translate the headers.

| File | Columns read directly by the base merge |
| --- | --- |
| EM-DAT | `DisNo.`, `ISO`, `Disaster Type`, `Country`, `Region`, `Event Name`, `Location`, `Start Year`, `Start Month`, `Start Day` |
| GDIS | `disasterno`, `iso3`, `latitude`, `longitude`, `geolocation`, `adm1`, `adm2`, `adm3`, `location` |

Keep the full EM-DAT export, not just those columns. Later modules use
`Latitude`, `Longitude`, administrative fields such as `Admin Units`/`GADM Admin Units`,
and impacts including `Total Deaths`, `Total Affected` and `Total Damage ('000 US$)`.
An empty cell is not the same as a missing required column. Leave unavailable
measurements empty; do not substitute zero for unknown coordinates or impacts.

To use other filenames, pass `--emdat-file PATH --gdis-file PATH` to the main.
Use the same overrides on subsequent commands. Derived directories are created
by the pipeline; GADM files are downloaded by the `gadm-download` step.

The release scope starts at **2014-04-03** by default. The base merge matches
EM-DAT/GDIS event and country identifiers. The subsequent geocoding stage reads
EM-DAT directly, so the release is not restricted to events covered by GDIS.

## Credentials and Service Configuration

### Hugging Face

Obtain access to [Llama 3.1 70B Instruct](https://huggingface.co/meta-llama/Llama-3.1-70B-Instruct)
and accept its conditions. Authenticate the account that will run the pipeline
with a token authorized to download the models:

```bash
hf auth login
hf auth whoami
```

If the CLI is missing, run `python -m pip install huggingface_hub` in the active
environment. See the [Hugging Face CLI guide](https://huggingface.co/docs/huggingface_hub/en/guides/cli).
Login alone does not grant gated-model access. Model files download on first use
when absent from the Hugging Face cache; account for that storage separately
from the dataset.

### Copernicus

Create a Copernicus Data Space Sentinel Hub OAuth client following the
[authentication guide](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html).
Use its **client ID and client secret**, not your login password or a GitHub token.

Enter them in the launch terminal without recording the secret in command history.
Bash:

```bash
read -r -p "Copernicus client ID: " COPERNICUS_CLIENT_ID
read -r -s -p "Copernicus client secret: " COPERNICUS_CLIENT_SECRET
printf '\n'
export COPERNICUS_CLIENT_ID COPERNICUS_CLIENT_SECRET
```

PowerShell:

```powershell
$env:COPERNICUS_CLIENT_ID = Read-Host 'Copernicus client ID'
$secret = Read-Host 'Copernicus client secret' -AsSecureString
$env:COPERNICUS_CLIENT_SECRET = [System.Net.NetworkCredential]::new('', $secret).Password
Remove-Variable secret
```

The code reads these environment variables but **does not automatically load a
`.env` file**. Set them in each new session or source a private configuration file
outside the repository. Protect its permissions and never commit credentials.
Check both Requests and Processing Units before launching satellite downloads.

### ReliefWeb and Nominatim

These settings are currently configured in Python files, not environment variables:

| Setting | Where to configure it |
| --- | --- |
| ReliefWeb appname | Request your own approved identifier through the [ReliefWeb API instructions](https://apidoc.reliefweb.int/parameters). Set `RELIEFWEB_APPNAME` in `src/support/constants.py` and use the same identifier for the ReliefWeb `appname`/`reliefweb_appname` defaults and the request in `query_africa_hazards` in `src/support/news_engine.py`. |
| Geocoder identity | Set `NOMINATIM_USER_AGENT` in `src/support/constants.py` to identify your application. |
| Geocoder endpoint | Set `NOMINATIM_SEARCH_URL` in `src/support/constants.py` to the search endpoint of your chosen compatible Nominatim service. The default is the public OSM endpoint. |

**Read the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/)
before using the public endpoint.** Large bulk jobs are discouraged. Permitted
small one-off batches must run on one thread/machine, cache results, and provide
attribution. Runs lasting over a day or repeated regularly are limited to four
requests per minute. The examples below use 16-second query and variant delays;
the code defaults to 1.2 seconds, which is not sufficient for those longer jobs.
Slower requests do not themselves authorize bulk use: choose a suitable
self-hosted or provider service for substantial workloads.

Open-Meteo and NASA POWER are called without API keys by the weather module.
Public services still have usage terms and quotas. In particular, several news
requests may be made per event; an event-level sleep alone does not enforce the
[ReliefWeb daily quota](https://apidoc.reliefweb.int/). Arrange suitable access or
run controlled batches, and inspect source failures before interpreting missing
articles as a genuine absence of news.

## Checks Before Running

With inputs, environment and credentials prepared:

```bash
nvidia-smi
python -c "import sys, torch, transformers, accelerate, bitsandbytes, pandas, openpyxl, rasterio, filelock; print(sys.executable); print('CUDA available:', torch.cuda.is_available()); print('GPU count:', torch.cuda.device_count())"
python -m unittest discover -s tests -v
python src/main.py release --help
python src/main.py release --list-steps
python src/main.py release
```

Tests use temporary fixtures without live API calls or LLM generation. A passing
test suite does not prove that a model fits in GPU memory or that remote access
and quotas are sufficient. Check the GPU memory reported by `nvidia-smi`; if
CUDA is unavailable, configure the GPU allocation/environment before launching
the default LLM stages. Without `--execute`, the main **only prints the plan**.
`Missing inputs now` is expected for derived files that preceding steps will
create, but not for your two raw inputs.

Check the input merge before any network retrieval or inference:

```bash
python src/main.py release --execute --steps base-merge
```

Inspect `results/disasters_per_satellite.csv`. Resolve missing columns, invalid
file formats or an empty merge before continuing. This step regenerates the base
CSV from your files; it makes no API calls and does not load an LLM.

## Run the Complete Pipeline

After completing the prerequisites, this command runs every stage, including
the final JSON/image packaging:

```bash
python src/main.py release --execute --steps all --nominatim-sleep-seconds 16 --nominatim-variant-sleep-seconds 16
```

Use provider-appropriate delays if you configured another Nominatim service.
The main runs **19 stages sequentially** and stops on a failing step. Successful
outputs remain on disk. The pipeline does not upload anything to Hugging Face.

For a long cluster run, use the site's scheduler or a persistent session. If
background processes are permitted within your allocation, this Bash alternative
captures the log and prints the process ID:

```bash
mkdir -p results/logs
nohup python -u src/main.py release --execute --steps all --nominatim-sleep-seconds 16 --nominatim-variant-sleep-seconds 16 > results/logs/pipeline.log 2>&1 &
echo $!
tail -f results/logs/pipeline.log
```

Choose **one** launch method; do not start both runs. `Ctrl+C` stops `tail`, not
the background process. `nohup` does not extend a cluster allocation. Do not
start another writer against the same output directories while one is active.

## Run Individual Modules

Use `--steps` to run only the required part. The main supplies the corresponding
scripts with consistent paths and model settings. These commands are alternatives
to `all`, not additional commands to run after a successful full pipeline.

**Prerequisite outputs must already exist.** Selecting a step/group does not
automatically run its dependencies. When building from scratch module by module,
follow the order below. Multiple step names are executed in pipeline order.

### Base Merge

Input: the two raw datasets. Output: `results/disasters_per_satellite.csv`.

```bash
python src/main.py release --execute --steps base-merge
```

### Geocoding

Run the complete module:

```bash
python src/main.py release --execute --steps geocoding --nominatim-sleep-seconds 16 --nominatim-variant-sleep-seconds 16
```

Or run its steps individually, in this order:

```bash
python src/main.py release --execute --steps geocoding-analyze
python src/main.py release --execute --steps gadm-download
python src/main.py release --execute --steps gadm-bboxes
python src/main.py release --execute --steps adm2-aois
python src/main.py release --execute --steps llm-location
python src/main.py release --execute --steps llm-nominatim-queries
python src/main.py release --execute --steps llm-nominatim-geocode --nominatim-sleep-seconds 16 --nominatim-variant-sleep-seconds 16
python src/main.py release --execute --steps geocoding-final
```

| Step | Work performed / main prerequisite |
| --- | --- |
| `geocoding-analyze` | Read EM-DAT dates, coordinates and administrative references; write candidates and admin-unit tables. |
| `gadm-download` | Download administrative geometries for the countries in the admin-unit table. |
| `gadm-bboxes` | Match those units to downloaded GADM geometry and build bounding boxes. |
| `adm2-aois` | Build event administrative areas from those matches. |
| `llm-location` | Use Llama to normalize the EM-DAT location text. Requires the configured model and GPU resources. |
| `llm-nominatim-queries` | Build deduplicated country-constrained queries from LLM locations and EM-DAT. |
| `llm-nominatim-geocode` | Submit queries to the configured service and save their results. |
| `geocoding-final` | Integrate direct EM-DAT coordinates, administrative fallback and LLM/Nominatim results using the review rules. |

Outputs, including coordinate provenance and review audits, are in
`results/recent_emdat_geocoding/`. The main coordinate table used downstream is
`emdat_2014_final_llm70b_review_resolved_v2.csv` in that directory.

### Weather

Input: the final geocoding CSV. Retrieve daily weather for accepted geocoded
events, with Open-Meteo first and NASA POWER as fallback:

```bash
python src/main.py release --execute --steps weather
```

Outputs: `results/weather/weather_2014_plus.json` and its `_progress.csv`.
The JSON is the weather module's checkpoint, not the complete multimodal dataset.
It preserves provider, units and missing observations for D-10 through D+10.

### News, Summaries and Causal Chains

Run the complete module after geocoding:

```bash
python src/main.py release --execute --steps reasoning
```

Or run its steps individually:

```bash
python src/main.py release --execute --steps news
python src/main.py release --execute --steps summary
python src/main.py release --execute --steps summary-validation
python src/main.py release --execute --steps causal-chain
python src/main.py release --execute --steps causal-type-normalization
```

| Step | Work performed / main prerequisite |
| --- | --- |
| `news` | Search for articles using event metadata from the final geocoding table; save articles, source information and progress. |
| `summary` | Filter the collected news for event relevance and generate summaries with Llama where input is usable. Requires news and event metadata. |
| `summary-validation` | Apply output checks to summaries and record accepted/insufficient/rejected outcomes. |
| `causal-chain` | Use Qwen to extract ordered causal steps with supporting quotes for events with accepted summaries; validate JSON and quote support. |
| `causal-type-normalization` | Normalize the retained step labels and save a mapping audit, without another LLM call. |

Outputs are in `results/news_reasoning/`: news JSON, summary and causal CSV/JSONL,
coverage tables, validated summaries and normalized causal types. Events with
no usable news can remain without a summary or chain. Automated acceptance is
not a guarantee of scientific correctness; inspect examples and audit fields.

### Satellite Collection

Input: accepted coordinates and normalized causal results. Run both preparation
and collection:

```bash
python src/main.py release --execute --steps satellite
```

Or run them separately:

```bash
python src/main.py release --execute --steps satellite-input
python src/main.py release --execute --steps satellite-general
```

`satellite-input` creates `events.csv` and selection audits in
`results/multimodal_satellite_2014_plus/`. `satellite-general` requires Copernicus
credentials and quota; it writes the batch summary, per-event manifests and
image files under that same directory. Downloads are sequential.

Before a large collection, after `satellite-input` has run, inspect the selection
and then test a small real batch:

```bash
python src/fetch_multimodal_satellite_batch.py --limit 3 --dry-run
python src/main.py release --execute --steps satellite-general --satellite-limit 3
```

The second command consumes quota. Check its manifests, sample images, disk use
and account consumption before running `satellite-general` without the limit.
The completed compatible downloads are reused.

The default area is approximately **20 km x 20 km around the event coordinate**,
not the whole disaster footprint. The time window is **21 days, D-10 to D+10**.
Products include:

- Sentinel-2: RGB, B12/B08/B04 false-color and ten-band raw GeoTIFFs, masks and PNG previews; the output grid is approximately 20 m.
- Sentinel-1: VV/VH radar GeoTIFFs, masks and PNG previews.
- Sentinel-3 SLSTR: thermal-band brightness temperatures in kelvin at approximately 1 km, **not a derived Land Surface Temperature product**.
- ESA WorldCover: one land-cover GeoTIFF per event from a 2020/2021 reference map, with its reference year recorded.

Not every sensor observes every event every day. `no_scene` is not a download
failure; available imagery can still be cloudy or show no visible damage.
No disaster-specific spectral indices are generated. Manifests record the actual
sensor/day availability and errors, rather than fabricating 21 images per product.

### Final Assembly and Packaging

After the preceding modules finish:

```bash
python src/main.py release --execute --steps final
```

Or run its two steps individually:

```bash
python src/main.py release --execute --steps final-dataset
python src/main.py release --execute --steps release-package
```

`final-dataset` joins the module outputs into an event-level CSV for inspection.
`release-package` selects the eligible intersection, writes the final JSON and
copies the associated images into a portable directory structure. It reads the
geocoding, weather, news, validated summary, normalized causal and satellite
outputs directly. No LLM calls, scientific downloads or uploads occur in export.

To inspect selection and create a one-event package before the full export:

```bash
python src/export_final_event_dataset.py --dry-run
python src/main.py release --execute --steps release-package --release-limit 1 --release-output-dir results/release_preview
```

Keep preview and full-release directories separate. Remove neither source
imagery nor checkpoints while an export is in progress.

## Selection and Resource Controls

Use `python src/main.py release --help` for the full option list. Important
controls, appended to any applicable main command, are:

| Option | Default / effect |
| --- | --- |
| `--start-date YYYY-MM-DD` | `2014-04-03`; lower bound for the event scope. |
| `--emdat-file PATH`, `--gdis-file PATH` | Override the two raw input paths. |
| `--location-model-name ID`, `--summary-model-name ID`, `--causal-model-name ID` | Override the model for each LLM stage. |
| `--llm-quantization 4bit` | Alternatives are `8bit` and `none`, with different memory requirements. |
| `--location-max-new-tokens N`, `--summary-max-new-tokens N`, `--causal-chain-max-new-tokens N` | Generation limits default to 32, 256 and 512 respectively; low limits can truncate responses, while higher limits increase work and memory demand. |
| `--location-limit N`, `--nominatim-limit N`, `--weather-limit N`, `--news-limit N`, `--summary-limit N`, `--causal-limit N`, `--satellite-limit N`, `--release-limit N` | `0` means no limit in the main. Limits select input rows, not necessarily N new successful results. |
| `--news-sleep-seconds N`, `--weather-sleep-seconds N`, `--satellite-sleep-seconds N` | Event-level delays; they do not replace provider-specific usage rules. |
| `--nominatim-sleep-seconds N`, `--nominatim-variant-sleep-seconds N` | Delays between queries and variants; configure both for the selected service. |
| `--satellite-all-events false` | Collect only events with accepted coordinates, usable dates and valid non-empty causal chains. |
| `--satellite-all-events true` | Collect all events with accepted coordinates and usable dates, without requiring a chain for collection. |
| `--satellite-disaster-type TYPE` | Optional type filter; no type filter by default, including Flood. |
| `--release-output-dir PATH` | Final package directory; defaults to `results/release`. |
| `--release-require-complete` | Exclude partial satellite collections from export; does not require an acquisition on every day. |

For example, collect satellites for all geocoded events instead of only those
with chains, regenerating the selection as well as running the downloads:

```bash
python src/main.py release --execute --steps satellite --satellite-all-events true
```

This flag does not change the final JSON selection: exported events still need
accepted coordinates, a valid non-empty causal chain and existing Sentinel
imagery. WorldCover or masks alone do not qualify an event. The export count is
computed from the actual intersection, not a fixed target number.

Use the same input/output overrides whenever resuming a stage. Changing a cutoff,
model or limit does not automatically rename output files or invalidate every
checkpoint. Use separate paths for experiments; do not mix test outputs with a
production run. Stage-specific output overrides are listed in `--help`.

## Resume and Troubleshoot

Resolve the reported error, restore the same environment/credentials, and resume
only the unfinished stages. The main does not globally skip all completed stages
when `all` is relaunched.

| Stage | What happens on a repeated run |
| --- | --- |
| `llm-location` | Reprocesses selected events and writes its CSV at the end; no per-event resume. Do not repeat a successful run unnecessarily. |
| `llm-nominatim-geocode` | Reuses saved query results; preserve the result CSV as the cache. |
| `weather` | Reuses compatible successful records and retries failures; retrying partial series requires the standalone `--retry-partial` option. |
| `news` | Skips current-version searched records, including zero-article results. Inspect source failures before explicitly retrying them. |
| `summary`, `causal-chain` | Skip IDs already saved in JSONL, including unsuccessful outcomes. A changed model/prompt does not automatically regenerate them. |
| `satellite-general` | Reuses compatible completed work and retries incomplete sensor/day downloads. Keep manifests with their files. |
| `release-package` | Resumes with matching inputs/configuration and verifies files. Use a separate directory if the inputs change. |

If only satellite collection and final packaging remain:

```bash
python src/main.py release --execute --steps satellite final
```

If news collection finished but summary generation was interrupted:

```bash
python src/main.py release --execute --steps summary summary-validation causal-chain causal-type-normalization satellite final
```

If only the package export failed:

```bash
python src/main.py release --execute --steps release-package
```

Do not use `--force` to fix quotas or authentication. In the main, that option
resets summary/causal JSONL outputs; it is not a universal resume flag. Changing
satellite configuration also requires compatible checkpoints or a separate output
directory. Never delete locks to bypass another writer.

| Symptom | Check |
| --- | --- |
| Missing Python module | Check `python -c "import sys; print(sys.executable)"`, activate the intended environment, and install requirements there. |
| Missing input / column | Check filenames, original headers, working directory, and whether prerequisite stages completed. |
| Hugging Face access denied | Check the running account's token and access to the configured gated model. |
| Copernicus `401` / `403` | Read the response: expired credentials, permissions and exhausted quota need different fixes. Repeated runs do not replenish quota. |
| GPU out of memory | Obtain adequate GPU/RAM resources or explicitly configure and validate another model. |
| No selected satellite events | Read `events_selection_audit.csv`; inspect dates, coordinate acceptance and saved causal steps. |
| `partial` / `error` satellite status | Inspect the event manifest and retry once the provider/storage problem is resolved. `no_data` means the search finished without usable sensor imagery. |
| Unexpected file in the release package | Keep logs and audits outside it. For recognized Jupyter checkpoints, close the editor tabs and add `--release-archive-jupyter-checkpoints` to the export command; they are archived outside the package. |

The satellite batch summary can contain multiple attempts for an event. Use the
per-event manifests for current completion status rather than counting CSV rows
as distinct successful events. `completed` does not mean all 21 days have images;
`skipped_existing` means a compatible finished checkpoint was reused.

## Outputs and Publication

Intermediate results remain available for inspection:

| Path | Contents |
| --- | --- |
| `results/disasters_per_satellite.csv` | Base EM-DAT/GDIS event table. |
| `results/recent_emdat_geocoding/` | Coordinate candidates, LLM locations, geocoder results, accepted coordinates and review audits. |
| `results/weather/` | Daily weather JSON and retrieval progress. |
| `results/news_reasoning/` | News, summaries, causal chains, validation/coverage tables and normalized-type audits. |
| `results/multimodal_satellite_2014_plus/` | Selected events, manifests, source satellite products and batch progress. |
| `results/final_environmental_causal_dataset_2014_plus.csv` | Joined event-level table across the geocoding scope; includes availability/status information even for events not eligible for export. |
| `results/final_environmental_causal_dataset_2014_plus_summary.csv` | Coverage summary of the joined table. |
| `results/release_selection.csv`, `results/release_export_report.json` | Export selections, exclusions and context warnings. |

The distributable dataset is the entire package:

```text
results/release/
  README.md
  package_manifest.json
  data/
    dataset.json
  images/
    <event_id>/
      <product>/
        day_00_YYYY-MM-DD.tif
        ...
```

`data/dataset.json` is keyed by event ID. Each record contains event identity,
coordinates and provenance, EM-DAT impacts, weather, relative satellite paths,
news references, a summary and causal steps. Unavailable values use `"none"`,
not invented zeros. Weather covers 21 days; pre/post aggregates each use the
10 days on their side of the event. News metrics are centralized under
`news_data.search_metadata`; summary and causal blocks retain content and LLM
execution/validation metadata. Full article text and model raw responses are
not part of the public JSON; causal steps retain their supporting quotes.

Images remain separate files referenced as `../images/...` relative to the JSON.
Keep `data/` and `images/` in this layout when transferring or uploading the
package. GeoTIFFs hold scientific data; PNGs, where present, are previews.

**Publish only when `package_manifest.json` has `status: complete`.** The exporter
checks file inventory, sizes and hashes. Manual edits after export invalidate
those checks. Inspect the export report and representative event records/images
before publication; integrity checks do not establish scientific correctness or
complete sensor coverage. See the [JSON schema and packaging reference](docs/final_release_dataset.md)
for field definitions and source/redistribution considerations.

The main does not upload files. Review source licenses and attribution before
publishing the package yourself. Keep the Git commit, raw-data versions, commands,
model/prompt identifiers and `python -m pip freeze` output with your experiment
records. Dependencies are not pinned and external sources can change, so running
the workflow later does not guarantee identical event counts or byte-for-byte
results.

## ✍️ Authors & Citation

**Francesco Scala, Liliana Martirano, Saverio Polito, Domenico Mandaglio and Luigi Pontieri.** *Institute of High Performance Computing and Networking (ICAR-CNR), Italy.*

If you use DisasterChain in your research, please cite:

 ```
   Coming soon...
 ```
