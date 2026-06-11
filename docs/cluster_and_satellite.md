# Cluster and Satellite Workflow

This note is the practical operating plan for the university cluster and the
first Sentinel-1/Sentinel-2 implementation.

## Recommended Workflow

Keep development local, then move code to the cluster only when you want to run
heavier jobs. I cannot directly edit files that live only inside the university
cluster from this local Codex workspace, unless the cluster filesystem is mounted
locally or a remote editing tool is added later.

The clean workflow is:

1. Edit and test small changes locally in this repository.
2. Commit/push changes with Git, or upload the project folder manually.
3. On the cluster, pull/download the updated project.
4. Run the scripts from your user directory.
5. Download only the generated outputs you need from `results/`.

Do not upload `.venv`, `.env`, or large generated satellite images to Git.

## First Cluster Setup

Open the JupyterLab terminal on the cluster and stay inside your own user area.
Exact paths depend on the cluster, so replace `<username>` and module names with
the values used by the university.

```bash
cd /users/<username>
mkdir -p projects venvs
```

If the cluster uses environment modules, inspect and load Python:

```bash
module avail python
module load python/3.11
python --version
```

Clone or upload the project:

```bash
cd /users/<username>/projects
git clone <repo-url> Tirocinio
cd Tirocinio
```

Create a virtual environment outside the project folder:

```bash
python -m venv /users/<username>/venvs/tirocinio
source /users/<username>/venvs/tirocinio/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Register it as a Jupyter kernel:

```bash
python -m ipykernel install --user --name tirocinio --display-name "Python (tirocinio)"
```

In notebooks, choose the kernel named `Python (tirocinio)`.

## Copernicus Credentials

Create an OAuth client in the Copernicus/Sentinel Hub dashboard, then keep the
client id and secret outside the repository.

Local PowerShell session:

```powershell
$env:COPERNICUS_CLIENT_ID="<client-id>"
$env:COPERNICUS_CLIENT_SECRET="<client-secret>"
```

These values disappear when the PowerShell window is closed. To avoid pasting
them repeatedly, keep a private file outside the repository, for example on the
Desktop:

```powershell
# C:\Users\<username>\Desktop\copernicus_env.ps1
$env:COPERNICUS_CLIENT_ID="<client-id>"
$env:COPERNICUS_CLIENT_SECRET="<client-secret>"
```

Then load it when needed:

```powershell
. C:\Users\<username>\Desktop\copernicus_env.ps1
```

For a single session:

```bash
export COPERNICUS_CLIENT_ID="<client-id>"
export COPERNICUS_CLIENT_SECRET="<client-secret>"
```

For repeated cluster sessions, create a private shell file:

```bash
nano ~/.copernicus_env
chmod 600 ~/.copernicus_env
source ~/.copernicus_env
```

The file should contain:

```bash
export COPERNICUS_CLIENT_ID="<client-id>"
export COPERNICUS_CLIENT_SECRET="<client-secret>"
```

Never commit secrets to Git.

## Run One Satellite Event

The first test event can come from `results/disasters_per_satellite.csv`:

```bash
source /users/<username>/venvs/tirocinio/bin/activate
cd /users/<username>/projects/Tirocinio
python src/fetch_satellite_event.py --event-id 2018-0040-BRA
```

The script writes outputs under:

```text
results/satellite/<event-id>/
```

Expected files include:

- `s1_pre_vv_vh.png`
- `s1_post_vv_vh.png`
- `s1_change_mask.png`
- `s2_pre_true_color.png`
- `s2_pre_mndwi_mask.png`
- `s2_pre_water_indices.tiff`
- `s2_post_true_color.png`
- `s2_post_mndwi_mask.png`
- `s2_post_water_indices.tiff`
- `s2_water_change_mask.png` when both Sentinel-2 scenes pass the local cloud
  usability threshold
- `manifest.json`

Manual event example:

```bash
python src/fetch_satellite_event.py \
  --event-id test-flood \
  --lat 44.50 \
  --lon 11.34 \
  --start-date 2023-05-17 \
  --aoi-half-size-km 10 \
  --max-cloud-cover 30
```

## Run A Small Satellite Batch

Before launching API requests, inspect which events would be processed:

```bash
cd /users/<username>/projects/Tirocinio
source /users/<username>/venvs/tirocinio/bin/activate
source ~/.copernicus_env
python src/fetch_satellite_batch.py --dry-run --limit 5
```

On the university cluster, a cautious first real batch is:

```bash
python src/fetch_satellite_batch.py \
  --limit 3 \
  --image-size 256 \
  --max-cloud-cover 70 \
  --window-days 30
```

The batch runner:

- selects flood events from `results/disasters_per_satellite.csv`;
- defaults to events from `2015-01-01` onward, because Sentinel-1/Sentinel-2
  availability is useful from that period;
- skips an event if `results/satellite/<event-id>/manifest.json` already exists;
- use `--force` only when you intentionally want to overwrite an existing event
  extraction;
- appends one row per event to `results/satellite/batch_summary.csv`, including
  S2 new-water area when the water-change mask is available.

Useful variants:

```bash
python src/fetch_satellite_batch.py --event-id 2016-0424-MEX --force
python src/fetch_satellite_batch.py --country Mexico --limit 5 --image-size 256
python src/fetch_satellite_batch.py --limit 0 --image-size 256 --sleep-seconds 2
```

Use `--limit 0` only after small runs look correct.

Rank the accumulated batch summary:

```bash
python src/rank_satellite_events.py --top 10
```

The ranking script writes `results/satellite/ranked_events.csv` and highlights
events with usable Sentinel-1/Sentinel-2 change layers, low local cloud cover,
and larger candidate new-water area. When manifest and mask files are available,
it also penalizes unreliable cloud/nodata pixels and candidate new water that is
mostly adjacent to persistent water, because those cases are often coastline,
river-edge, tide, or registration artefacts rather than clear flood extent.

## What The First Version Does

Sentinel-1:

- searches `sentinel-1-grd`;
- filters IW mode and DV polarization, which corresponds to VV+VH;
- requests orthorectified GRD with Copernicus DEM and terrain gamma0;
- saves pre/post VV/VH PNG composites;
- creates a simple local radar-darkening mask from pre/post VV.

Sentinel-2:

- searches `sentinel-2-l2a`;
- filters by tile-level `eo:cloud_cover`;
- saves true-color RGB PNGs;
- saves MNDWI water-mask PNGs;
- saves MNDWI, NDWI, and dataMask bands in a GeoTIFF.
- when both pre/post scenes are locally usable, saves a water-change PNG where:
  - blue means candidate new post-event water;
  - teal means persistent water;
  - orange means water present only before the event;
  - white/black means unreliable cloud/nodata pixels.

The generated masks are first-pass candidates for inspection, not final validated
flood labels.
