from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from main import fetch_weather_data
from support.constants import WEATHER_VARIABLES
from support.utils import calculate_weather_summaries


DEFAULT_INPUT_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_OUTPUT_JSON = "results/weather/weather_2014_plus.json"
DEFAULT_PROGRESS_CSV = "results/weather/weather_2014_plus_progress.csv"
WEATHER_BATCH_VERSION = "weather_batch_v1"


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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# Writes the weather checkpoint after each processed event.
def save_json(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# Converts the final geocoding table into weather-ready event rows.
def weather_events(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    event_column = first_column(frame.columns, ("event_id", "emdat_disaster_id", "DisNo."))
    country_column = first_column(frame.columns, ("country", "Country"))
    type_column = first_column(frame.columns, ("disaster_type", "Disaster Type"))
    start_column = first_column(frame.columns, ("start_date", "_llm_start_date", "Start Date"))
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
    for _, row in frame.iterrows():
        event_id = clean_text(row.get(event_column))
        start_date = pd.to_datetime(row.get(start_column), errors="coerce")
        latitude = pd.to_numeric(row.get(lat_column), errors="coerce")
        longitude = pd.to_numeric(row.get(lon_column), errors="coerce")
        if not event_id or pd.isna(start_date) or pd.isna(latitude) or pd.isna(longitude):
            continue

        start = start_date.to_pydatetime()
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
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [progress_row(record) for record in records.values()]
    fieldnames = list(progress_row({}).keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# Defines the command-line options for weather batch retrieval.
def parse_args() -> argparse.Namespace:
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
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


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
    if args.offset:
        events = events[args.offset :]
    if args.limit > 0:
        events = events[: args.limit]

    records = load_json(output_json)
    cache: Dict[tuple, Any] = {}
    for record in records.values():
        daily = ((record.get("weather_data") or {}).get("daily_series"))
        if daily:
            cache_key = (
                record.get("latitude"),
                record.get("longitude"),
                record.get("date_minus_10"),
                record.get("date_plus_10"),
            )
            cache[cache_key] = daily

    processed = 0
    skipped = 0
    failed = 0
    total = len(events)
    for index, event in enumerate(events, start=1):
        existing = records.get(event["event_id"])
        if (
            existing
            and not args.force
            and existing.get("weather_batch_version") == WEATHER_BATCH_VERSION
            and existing.get("weather_retrieval_status") in {"fetched", "failed"}
        ):
            skipped += 1
            continue

        print(f"[{index}/{total}] {event['event_id']}: {event['start_date']}", flush=True)
        if args.dry_run:
            records[event["event_id"]] = {
                **event,
                "weather_retrieval_status": "dry_run",
                "weather_data": None,
                "weather_batch_version": WEATHER_BATCH_VERSION,
            }
            processed += 1
            continue

        cache_key = (
            event["latitude"],
            event["longitude"],
            event["date_minus_10"],
            event["date_plus_10"],
        )
        daily_series = cache.get(cache_key)
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
                cache[cache_key] = daily_series

        if daily_series is None:
            failed += 1
            status = "failed"
            pre_summary, post_summary = None, None
        else:
            status = "fetched"
            pre_summary, post_summary = calculate_weather_summaries(daily_series)

        records[event["event_id"]] = {
            **event,
            "weather_retrieval_status": status,
            "weather_data": {
                "pre_event_summary": pre_summary,
                "post_event_summary": post_summary,
                "daily_series": daily_series,
            },
            "weather_batch_version": WEATHER_BATCH_VERSION,
        }
        save_json(output_json, records)
        write_progress_csv(progress_csv, records)
        processed += 1

        if index < total and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    write_progress_csv(progress_csv, records)
    fetched = sum(
        1 for record in records.values() if record.get("weather_retrieval_status") == "fetched"
    )
    print()
    print("Input events:", len(frame))
    print("Selected geocoded events:", total)
    print("Processed this run:", processed)
    print("Skipped existing current-version weather:", skipped)
    print("Failed this run:", failed)
    print("Total weather records:", len(records))
    print("Fetched weather records:", fetched)
    print("Output JSON:", output_json)
    print("Progress CSV:", progress_csv)


if __name__ == "__main__":
    main()
