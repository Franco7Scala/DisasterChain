"""Package approved event records and existing satellite files for manual publication."""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from filelock import FileLock

from run_release_pipeline import (
    DEFAULT_FINAL_GEOCODING_CSV, DEFAULT_NEWS_JSON, DEFAULT_NORMALIZED_CAUSAL_CSV,
    DEFAULT_VALIDATED_SUMMARY_CSV, DEFAULT_WEATHER_JSON,
)
from support.final_dataset_records import (
    POSITION_SOURCES, causal_fields, event_date, event_fields, news_fields, number,
    public_values, read_json, read_records, summary_fields, weather_fields,
)
from support.final_dataset_satellite import SatelliteRejected, satellite_fields
from support.satellite_selection import causal_chain_status


SCHEMA_VERSION = "environmental-causal-release-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = "results/release"


# Resolves CLI paths consistently even when the caller is outside the project directory.
def project_path(value):
    path = Path(value)
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Publishes a complete file atomically without leaving a truncated JSON or report.
def atomic_write(path, content, staging_dir=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=staging_dir or path.parent, prefix=".dataset-export-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Avoids joining meteorological measurements collected for another date or coordinate.
def compatible_context(record, row, start, *, coordinates=False):
    saved_date = event_date(record)
    if saved_date is not None and saved_date != start:
        return False
    if coordinates:
        for field in ("latitude", "longitude"):
            value = number(record.get(field))
            if value is not None and abs(value - float(row[field])) > 1e-6:
                return False
    return True


# Builds the requested intersection and an explicit reason for every excluded event.
def build_export(args, progress=None, *, selection_only=False):
    paths = {name: project_path(getattr(args, name)) for name in
             ("geocoding_csv", "causal_csv", "weather_json", "news_json", "summary_csv")}
    fingerprints = {}
    inputs = {}
    for name, path in paths.items():
        if selection_only and name in {"weather_json", "news_json", "summary_csv"}:
            inputs[name] = {}
            fingerprints[name] = "none"
            continue
        if not path.exists() and args.allow_missing_context and name in {"weather_json", "news_json", "summary_csv"}:
            inputs[name] = {}
            fingerprints[name] = "none"
            continue
        fingerprints[name] = file_hash(path)
        inputs[name] = read_records(path)
    geo = inputs["geocoding_csv"]
    requested = set(args.event_id or [])
    if requested - geo.keys():
        raise ValueError("Unknown event id(s): " + ", ".join(sorted(requested - geo.keys())))
    root = project_path(args.satellite_dir)
    output, assets, audit, warnings, manifests = {}, [], [], {}, {}
    for index, (event_id, row) in enumerate(geo.items(), start=1):
        if progress and index % 100 == 0:
            progress(index, len(geo), len(output))
        if requested and event_id not in requested:
            continue
        status = "selected"
        lat, lon = number(row.get("latitude")), number(row.get("longitude"))
        start = event_date(row)
        causal = inputs["causal_csv"].get(event_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", event_id):
            raise ValueError(f"Unsafe event id: {event_id!r}")
        if row.get("position_source") not in POSITION_SOURCES or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            status = "missing_accepted_coordinates"
        elif start is None:
            status = "missing_event_date"
        elif causal is None or causal_chain_status(causal) != "valid":
            status = "no_valid_causal_chain"
        elif not compatible_context(causal, row, start):
            status = "causal_event_date_mismatch"
        if status == "selected" and args.limit and len(output) >= args.limit:
            status = "not_evaluated_limit"
        manifest_path = root / event_id / "manifest.json"
        if status == "selected":
            if not manifest_path.is_file():
                status = "missing_satellite_manifest"
            else:
                try:
                    if not manifest_path.resolve().is_relative_to(root):
                        raise SatelliteRejected("manifest_outside_satellite_directory")
                    digest = file_hash(manifest_path)
                    satellite, event_assets = satellite_fields(
                        read_json(manifest_path), event_id, row, start, manifest_path.parent,
                        PROJECT_ROOT, require_complete=args.require_complete,
                    )
                except (SatelliteRejected, OSError, ValueError, TypeError, KeyError) as exc:
                    status = "satellite_rejected: " + str(exc)
        audit.append({"event_id": event_id, "selection_status": status})
        if status != "selected":
            continue
        if selection_only:
            output[event_id] = event_fields(event_id, row, start)
            continue
        notes = []
        context = {}
        for name, coordinate_check in (("weather_json", True), ("news_json", False), ("summary_csv", False)):
            record = inputs[name].get(event_id)
            if record and not compatible_context(record, row, start, coordinates=coordinate_check):
                notes.append(name + "_event_mismatch")
                record = None
            context[name] = record
        try:
            output[event_id] = public_values({
                **event_fields(event_id, row, start),
                "weather_data": weather_fields(context["weather_json"], start, notes),
                "satellite_data": satellite,
                "news_data": news_fields(context["news_json"], notes),
                "causal_chain": causal_fields(causal),
                "summary": summary_fields(context["summary_csv"], notes),
            })
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Invalid context for {event_id}: {exc}") from exc
        if satellite["status"] == "partial":
            notes.append("partial_satellite_collection_included")
        if notes:
            warnings[event_id] = sorted(set(notes))
        assets.extend(event_assets)
        manifests[str(manifest_path)] = digest
    return output, assets, audit, warnings, paths, fingerprints, manifests


# Refuses a release directory that could overwrite scientific inputs or source imagery.
def check_output_directory(output_dir, inputs, satellite_dir):
    if (output_dir == PROJECT_ROOT or output_dir in PROJECT_ROOT.parents
            or output_dir.is_relative_to(satellite_dir) or satellite_dir.is_relative_to(output_dir)
            or any(path.is_relative_to(output_dir) for path in inputs.values())):
        raise ValueError("Use a separate release directory, outside all source inputs")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("Release destination is not a directory")


# Copies into a temporary file and verifies its bytes before exposing the public asset.
def copy_asset(asset, target, expected_hash, staging_dir):
    if target.exists():
        if target.is_symlink() or not target.is_file() or file_hash(target) != expected_hash:
            raise ValueError(f"Existing asset differs; use a new output directory: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=staging_dir, prefix=".dataset-export-", delete=False) as handle:
            temporary = Path(handle.name)
        shutil.copyfile(asset.source, temporary)
        if temporary.stat().st_size != asset.size or file_hash(temporary) != expected_hash:
            raise ValueError(f"Source changed while copying: {asset.source}")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Packages only the selected files and resumes only an identical export plan.
def write_package(args, result):
    output, assets, audit, warnings, paths, fingerprints, manifests = result
    destination = project_path(args.output_dir)
    check_output_directory(destination, paths, project_path(args.satellite_dir))
    if not output:
        raise ValueError("No eligible events; inspect the selection counts before exporting")
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(destination.parent / ("." + destination.name + ".export.lock")), timeout=0)
    with lock:
        if destination.exists() and destination.stat().st_dev != destination.parent.stat().st_dev:
            raise ValueError("Use a release subdirectory on the same filesystem as its parent")
        dataset = json_bytes(output)
        print(f"Checking {len(assets)} source files before packaging...", flush=True)
        entries = [{"path": asset.destination, "bytes": asset.size, "sha256": file_hash(asset.source)}
                   for asset in assets]
        plan = {"schema_version": SCHEMA_VERSION, "event_ids": list(output),
                "event_count": len(output), "require_complete": args.require_complete,
                "dataset_path": "data/dataset.json", "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
                "input_sha256": fingerprints, "assets": entries}
        manifest_path = destination / "package_manifest.json"
        if destination.exists() and any(destination.iterdir()):
            if not manifest_path.is_file():
                raise ValueError("Non-empty unmanaged release directory; choose a new --output-dir")
            previous = read_json(manifest_path)
            previous.pop("status", None)
            if previous != plan:
                raise ValueError("Export selection or inputs changed; choose a new --output-dir")
        allowed = {entry["path"] for entry in entries} | {"data/dataset.json", "package_manifest.json", "README.md"}
        for path in destination.rglob("*") if destination.exists() else []:
            if path.is_symlink() or path.is_file() and path.relative_to(destination).as_posix() not in allowed:
                raise ValueError(f"Unexpected file in release directory: {path}")
        needed = sum(asset.size for asset in assets if not (destination / asset.destination).exists())
        if shutil.disk_usage(destination.parent).free < needed + len(dataset) + max((asset.size for asset in assets), default=0):
            raise ValueError("Not enough free disk space to copy the release images")
        atomic_write(manifest_path, json_bytes({**plan, "status": "incomplete"}), destination.parent)
        for index, (asset, entry) in enumerate(zip(assets, entries), start=1):
            copy_asset(asset, destination / asset.destination, entry["sha256"], destination.parent)
            if index % 100 == 0 or index == len(assets):
                print(f"Images verified/copied: {index}/{len(assets)}", flush=True)
        for name, path in paths.items():
            if fingerprints[name] != "none" and file_hash(path) != fingerprints[name]:
                raise ValueError(f"Input changed during export: {path}")
        for path, digest in manifests.items():
            if file_hash(path) != digest:
                raise ValueError(f"Satellite manifest changed during export: {path}")
        documentation = (PROJECT_ROOT / "docs" / "final_release_dataset.md").read_bytes()
        atomic_write(destination / "README.md", documentation, destination.parent)
        atomic_write(destination / "data" / "dataset.json", dataset, destination.parent)
        atomic_write(manifest_path, json_bytes({**plan, "status": "complete"}), destination.parent)
        report = {"schema_version": SCHEMA_VERSION, "exported_events": len(output),
                  "selection_counts": dict(Counter(row["selection_status"] for row in audit)),
                  "image_files": len(assets), "image_bytes": sum(asset.size for asset in assets),
                  "warnings": warnings}
        atomic_write(destination.parent / (destination.name + "_export_report.json"), json_bytes(report))
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=["event_id", "selection_status"])
        writer.writeheader()
        writer.writerows(audit)
        atomic_write(destination.parent / (destination.name + "_selection.csv"), stream.getvalue().encode("utf-8"))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geocoding-csv", default=DEFAULT_FINAL_GEOCODING_CSV)
    parser.add_argument("--causal-csv", default=DEFAULT_NORMALIZED_CAUSAL_CSV)
    parser.add_argument("--weather-json", default=DEFAULT_WEATHER_JSON)
    parser.add_argument("--news-json", default=DEFAULT_NEWS_JSON)
    parser.add_argument("--summary-csv", default=DEFAULT_VALIDATED_SUMMARY_CSV)
    parser.add_argument("--satellite-dir", default="results/multimodal_satellite_2014_plus")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--event-id", action="append", help="Export only this event; repeat for several ids")
    parser.add_argument("--limit", type=int, default=0, help="Limit eligible events, not scanned input rows; 0 means all")
    parser.add_argument("--require-complete", action="store_true", help="Exclude partial collections even if images exist")
    parser.add_argument("--allow-missing-context", action="store_true", help="Explicitly allow missing weather/news/summary files")
    parser.add_argument("--dry-run", action="store_true", help="Check the real inputs without writing or copying files")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


def main():
    args = parse_args()
    try:
        print("Reading saved data and checking manifests; no downloads or model calls.", flush=True)
        result = build_export(args, progress=lambda index, total, selected: print(
            f"Input rows scanned: {index}/{total}; selected: {selected}", flush=True))
        output, assets, audit, warnings, *_ = result
        print("Selection:", dict(Counter(row["selection_status"] for row in audit)))
        print("Exportable events:", len(output))
        print("Image files:", len(assets))
        print("Image bytes:", sum(asset.size for asset in assets))
        print("Events with context warnings:", len(warnings))
        for event_id, notes in list(warnings.items())[:5]:
            print(f"  {event_id}: " + "; ".join(notes))
        if not output:
            raise ValueError("No eligible events; no release was written")
        if args.dry_run:
            print("Dry run: no API calls, output files or image copies.")
            print("Selected event ids:", ", ".join(list(output)[:10]))
            return
        write_package(args, result)
        destination = project_path(args.output_dir)
        print("Final complete JSON:", destination / "data" / "dataset.json")
        print("Satellite images:", destination / "images")
        print("Package verification manifest:", destination / "package_manifest.json")
        print("Selection audit:", destination.parent / (destination.name + "_selection.csv"))
        print("Export report:", destination.parent / (destination.name + "_export_report.json"))
        print("No files were uploaded. Publish only a package whose manifest status is complete.")
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
