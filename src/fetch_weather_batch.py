from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from filelock import FileLock

from main import fetch_weather_data
from support.constants import WEATHER_VARIABLES
from support.utils import calculate_weather_summaries
from support.final_dataset_records import POSITION_SOURCES, event_date, read_records
from support.satellite_selection import DEFAULT_CAUSAL_CSV
from support.weather_data import requested_dates, valid_day_counts


DEFAULT_INPUT_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_OUTPUT_JSON = "results/weather/weather_2014_plus.json"
DEFAULT_PROGRESS_CSV = "results/weather/weather_2014_plus_progress.csv"
WEATHER_BATCH_VERSION = "weather_batch_v2"


# Returns the first available column from a list of possible names.
def first_column(columns: Iterable[str], names: Iterable[str]) -> Optional[str]:
    available = set(columns)
    for name in names:
        if name in available:
            return name
    return None


# Cleans a scalar value for output and comparisons.
def clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "[]"}:
        return ""
    return " ".join(text.replace("\n", " ").replace("\r", " ").split())


# Loads a resumable weather checkpoint from disk.
def load_json(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    return read_records(path)


# Replaces a completed checkpoint atomically so an interruption preserves the previous version.
def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Writes the weather checkpoint after each processed event.
def save_json(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    atomic_text(path, json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))


# Converts the final geocoding table into weather-ready event rows.
def weather_events(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    event_column = first_column(frame.columns, ("event_id", "emdat_disaster_id", "DisNo."))
    country_column = first_column(frame.columns, ("country", "Country"))
    type_column = first_column(frame.columns, ("disaster_type", "Disaster Type"))
    start_column = first_column(frame.columns, ("event_date", "start_date", "_llm_start_date", "Start Date"))
    if start_column is None and {"Start Year", "Start Month", "Start Day"}.issubset(frame.columns):
        start_column = "Start Year"
    location_column = first_column(frame.columns, ("location", "Location", "emdat_location"))
    source_column = first_column(frame.columns, ("position_source",))
    lat_column = first_column(frame.columns, ("latitude", "Latitude", "final_latitude"))
    lon_column = first_column(frame.columns, ("longitude", "Longitude", "final_longitude"))

    required = {
        "event id": event_column,
        "start date": start_column,
        "latitude": lat_column,
        "longitude": lon_column,
    }
    missing = [label for label, column in required.items() if column is None]
    if missing:
        raise SystemExit("Input CSV missing column(s): " + ", ".join(missing))

    rows = []
    seen = set()
    for _, row in frame.iterrows():
        event_id = clean_text(row.get(event_column))
        if event_id in seen:
            raise ValueError(f"Duplicate weather event id: {event_id}")
        if event_id:
            seen.add(event_id)
        start = event_date(row)
        latitude = pd.to_numeric(row.get(lat_column), errors="coerce")
        longitude = pd.to_numeric(row.get(lon_column), errors="coerce")
        if not event_id or start is None or pd.isna(latitude) or pd.isna(longitude):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        if source_column and clean_text(row.get(source_column)) not in POSITION_SOURCES:
            continue

        rows.append(
            {
                "event_id": event_id,
                "country": clean_text(row.get(country_column)) if country_column else "",
                "disaster_type": clean_text(row.get(type_column)) if type_column else "",
                "location": clean_text(row.get(location_column)) if location_column else "",
                "position_source": clean_text(row.get(source_column)) if source_column else "",
                "latitude": float(latitude),
                "longitude": float(longitude),
                "start_date": start.strftime("%Y-%m-%d"),
                "date_minus_10": (start - timedelta(days=10)).strftime("%Y-%m-%d"),
                "date_plus_10": (start + timedelta(days=10)).strftime("%Y-%m-%d"),
            }
        )
    return rows


# Builds a compact progress row for the CSV report.
def progress_row(record: Dict[str, Any]) -> Dict[str, Any]:
    weather = record.get("weather_data") or {}
    pre = weather.get("pre_event_summary") or {}
    post = weather.get("post_event_summary") or {}
    return {
        "event_id": record.get("event_id", ""),
        "country": record.get("country", ""),
        "disaster_type": record.get("disaster_type", ""),
        "start_date": record.get("start_date", ""),
        "position_source": record.get("position_source", ""),
        "latitude": record.get("latitude", ""),
        "longitude": record.get("longitude", ""),
        "weather_retrieval_status": record.get("weather_retrieval_status", ""),
        "weather_provider": weather.get("provider", ""),
        **{f"valid_days_{key}": count for key, count in valid_day_counts(weather.get("daily_series")).items()},
        "pre_total_rainfall_mm": pre.get("total_rainfall_mm", ""),
        "pre_max_daily_rainfall_mm": pre.get("max_daily_rainfall_mm", ""),
        "pre_avg_max_temperature_c": pre.get("avg_max_temperature_c", ""),
        "post_total_rainfall_mm": post.get("total_rainfall_mm", ""),
        "post_max_daily_rainfall_mm": post.get("max_daily_rainfall_mm", ""),
        "post_avg_max_temperature_c": post.get("avg_max_temperature_c", ""),
        "weather_batch_version": record.get("weather_batch_version", ""),
    }


# Writes a compact CSV report from the current JSON checkpoint.
def write_progress_csv(path: Path, records: Dict[str, Dict[str, Any]]) -> None:
    rows = [progress_row(record) for record in records.values()]
    fieldnames = list(progress_row({}).keys())
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, stream.getvalue())


# Defines the command-line options for weather batch retrieval.
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Open-Meteo/NASA POWER weather data for geocoded events."
    )
    parser.add_argument("--input-csv", default=DEFAULT_INPUT_CSV)
    parser.add_argument("--output-json", default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--progress-csv", default=DEFAULT_PROGRESS_CSV)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-partial", action="store_true", help="Retry saved partial series as well as failures")
    parser.add_argument("--release-events-only", action="store_true", help="Use the final coordinate/chain/image intersection")
    parser.add_argument("--causal-csv", default=DEFAULT_CAUSAL_CSV)
    parser.add_argument("--satellite-dir", default="results/multimodal_satellite_2014_plus")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if min(args.offset, args.limit, args.max_retries, args.sleep_seconds) < 0:
        parser.error("Limits, retry counts and sleep must be non-negative")
    return args


# Uses the exporter's selection without requiring weather, news or summaries to exist first.
def release_event_ids(args):
    from export_final_event_dataset import build_export, parse_args as export_args

    selection = export_args(["--geocoding-csv", args.input_csv, "--causal-csv", args.causal_csv,
                             "--satellite-dir", args.satellite_dir])
    selected, _, audit, *_ = build_export(selection, selection_only=True, progress=lambda i, total, kept: print(
        f"Selection checked: {i}/{total}; eligible: {kept}", flush=True))
    print("Release selection:", dict(Counter(row["selection_status"] for row in audit)))
    return set(selected)


def cache_key(record):
    return tuple(record.get(key) for key in ("latitude", "longitude", "date_minus_10", "date_plus_10"))


# Reuses only current successful records whose dates, coordinates and series still agree.
def reusable(record, event, *, retry_partial=False):
    statuses = {"fetched"} if retry_partial else {"fetched", "partial"}
    if not record or record.get("weather_batch_version") != WEATHER_BATCH_VERSION:
        return False
    if record.get("weather_retrieval_status") not in statuses or cache_key(record) != cache_key(event):
        return False
    weather = record.get("weather_data") or {}
    daily = weather.get("daily_series") or {}
    try:
        expected = requested_dates({"start_date": event["date_minus_10"], "end_date": event["date_plus_10"]})
    except (KeyError, ValueError, TypeError):
        return False
    if daily.get("time") != expected or any(not isinstance(daily.get(key), list) or len(daily[key]) != len(expected)
                                             for key in WEATHER_VARIABLES):
        return False
    counts = valid_day_counts(daily)
    return bool(daily.get("provider")) and (all(value == len(expected) for value in counts.values())
                                            if record.get("weather_retrieval_status") == "fetched"
                                            else any(counts.values()))


# Retrieves weather data incrementally for all selected geocoded events.
def main() -> None:
    args = parse_args()
    input_csv = Path(args.input_csv)
    output_json = Path(args.output_json)
    progress_csv = Path(args.progress_csv)
    if not input_csv.exists():
        raise SystemExit(f"Input CSV not found: {input_csv}")

    frame = pd.read_csv(input_csv, low_memory=False)
    events = weather_events(frame)
    if args.release_events_only:
        selected = release_event_ids(args)
        events = [event for event in events if event["event_id"] in selected]
        if {event["event_id"] for event in events} != selected:
            raise ValueError("Weather input dates do not cover every release-selected event")
    if args.offset:
        events = events[args.offset :]
    if args.limit > 0:
        events = events[: args.limit]

    print("Input events:", len(frame))
    print("Selected geocoded events:", len(events))
    if not events:
        raise SystemExit("No valid weather events selected")
    protected = {input_csv.resolve(), Path(args.causal_csv).resolve()}
    if output_json.resolve() == progress_csv.resolve() or {output_json.resolve(), progress_csv.resolve()} & protected:
        raise ValueError("Weather outputs must be separate from each other and from source inputs")
    if args.dry_run:
        for event in events[:10]:
            print(event["event_id"], event["start_date"], event["latitude"], event["longitude"])
        print("Dry run: no API calls, checkpoints or progress files written.")
        return
    output_json.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output_json) + ".lock", timeout=0):
        run_batch(args, events)


# Checkpoints each event and retries failed requests on the next run without replacing valid data.
def run_batch(args, events) -> None:
    output_json, progress_csv = Path(args.output_json), Path(args.progress_csv)

    records = load_json(output_json)
    cache: Dict[tuple, Any] = {}
    for record in records.values():
        if not args.force and reusable(record, record, retry_partial=args.retry_partial):
            cache[cache_key(record)] = record["weather_data"]["daily_series"]

    processed = 0
    skipped = 0
    failed = 0
    total = len(events)
    for index, event in enumerate(events, start=1):
        existing = records.get(event["event_id"])
        if (
            existing
            and not args.force
            and reusable(existing, event, retry_partial=args.retry_partial)
        ):
            skipped += 1
            continue

        print(f"[{index}/{total}] {event['event_id']}: {event['start_date']}", flush=True)
        key = cache_key(event)
        daily_series = None if args.force else cache.get(key)
        if daily_series is None:
            api_parameters = {
                "latitude": event["latitude"],
                "longitude": event["longitude"],
                "start_date": event["date_minus_10"],
                "end_date": event["date_plus_10"],
                "daily": WEATHER_VARIABLES,
                "timezone": "auto",
            }
            daily_series = fetch_weather_data(
                api_parameters,
                event["event_id"],
                index,
                max_retries=args.max_retries,
            )
            if daily_series is not None:
                cache[key] = daily_series

        counts = valid_day_counts(daily_series)
        if not any(counts.values()):
            failed += 1
            status = "failed"
            pre_summary, post_summary = None, None
        else:
            status = "fetched" if all(value == 21 for value in counts.values()) else "partial"
            pre_summary, post_summary = calculate_weather_summaries(daily_series)

        result = {
            **event,
            "weather_retrieval_status": status,
            "weather_data": {
                "provider": (daily_series or {}).get("provider"),
                "units": (daily_series or {}).get("units", {}),
                "time_basis": (daily_series or {}).get("time_basis"),
                "valid_days": counts,
                "pre_event_summary": pre_summary,
                "post_event_summary": post_summary,
                "daily_series": daily_series,
            },
            "weather_batch_version": WEATHER_BATCH_VERSION,
        }
        previous_counts = valid_day_counts((existing or {}).get("weather_data", {}).get("daily_series")) if reusable(existing, event) else {}
        if previous_counts and any(counts[key] < previous_counts[key] for key in WEATHER_VARIABLES):
            print("  Retry has less coverage; preserving the previously downloaded weather.", flush=True)
        else:
            records[event["event_id"]] = result
        save_json(output_json, records)
        write_progress_csv(progress_csv, records)
        processed += 1
        print(f"  {event['event_id']}: {status}; valid days: {counts}", flush=True)

        if index < total and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    write_progress_csv(progress_csv, records)
    fetched = sum(
        1 for record in records.values() if record.get("weather_retrieval_status") == "fetched"
    )
    print()
    print("Selected geocoded events:", total)
    print("Processed this run:", processed)
    print("Skipped existing current-version weather:", skipped)
    print("Failed this run:", failed)
    print("Total weather records:", len(records))
    print("Fetched weather records:", fetched)
    print("Partial weather records:", sum(record.get("weather_retrieval_status") == "partial" for record in records.values()))
    print("Output JSON:", output_json)
    print("Progress CSV:", progress_csv)


if __name__ == "__main__":
    main()
