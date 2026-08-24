from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from support.constants import RECENT_EMDAT_GEOCODING_OUTPUT_DIR, RELIEFWEB_APPNAME, RESULTS_DIR
from support.event_news_summary import clean_text
from support.utils import load_checkpoint, save_checkpoint


DEFAULT_INPUT_CSV = (
    Path(RECENT_EMDAT_GEOCODING_OUTPUT_DIR)
    / "emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_OUTPUT_JSON = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "final_environmental_causal_dataset_2014_plus_news.json"
)
DEFAULT_PROGRESS_CSV = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "final_environmental_causal_dataset_2014_plus_news_progress.csv"
)

EVENT_ID_COLUMNS = ("DisNo.", "disaster_id", "emdat_disaster_id")
LOCATION_CONTEXT_FIELDS = {
    "event_name": ("Event Name", "event_name"),
    "emdat_location": ("Location", "emdat_location"),
    "geolocation": ("geolocation",),
    "adm1": ("adm1", "ADM1", "admin1"),
    "adm2": ("adm2", "ADM2", "admin2"),
    "adm3": ("adm3", "ADM3", "admin3"),
    "location": ("location",),
}


# Reads the final geocoded event CSV as strings to preserve identifiers.
def read_events(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"Input CSV not found: {path}")
    return pd.read_csv(path, dtype=object)


# Returns the first available value among multiple possible column names.
def first_value(row: Mapping[str, Any], names: Iterable[str]) -> str:
    for name in names:
        value = clean_text(row.get(name))
        if value:
            return value
    return ""


# Finds the event id column used by the input file.
def event_id_column(frame: pd.DataFrame) -> str:
    for column in EVENT_ID_COLUMNS:
        if column in frame.columns:
            return column
    raise SystemExit("Input CSV has no event id column")


# Builds a YYYY-MM-DD start date from either an existing date or EM-DAT date parts.
def start_date_from_row(row: Mapping[str, Any]) -> str:
    direct = first_value(row, ("start_date", "Start Date", "_llm_start_date"))
    parsed = pd.to_datetime(direct, errors="coerce") if direct else pd.NaT
    if pd.notna(parsed):
        return parsed.strftime("%Y-%m-%d")

    year = first_value(row, ("Start Year", "start_year"))
    month = first_value(row, ("Start Month", "start_month")) or "1"
    day = first_value(row, ("Start Day", "start_day")) or "1"
    parsed = pd.to_datetime(
        {"year": [year], "month": [month], "day": [day]},
        errors="coerce",
    )[0]
    return parsed.strftime("%Y-%m-%d") if pd.notna(parsed) else ""


# Builds the location fields used by the news search engine.
def location_context_from_row(row: Mapping[str, Any]) -> Dict[str, str]:
    return {
        output_key: first_value(row, source_keys)
        for output_key, source_keys in LOCATION_CONTEXT_FIELDS.items()
    }


# Converts one final geocoding CSV row into the JSON record used by news and summary.
def record_from_row(row: Mapping[str, Any], event_id: str) -> Dict[str, Any]:
    start_date = start_date_from_row(row)
    start_dt = pd.to_datetime(start_date, errors="coerce") if start_date else pd.NaT
    date_minus_10 = ""
    date_plus_10 = ""
    if pd.notna(start_dt):
        date_minus_10 = (start_dt - timedelta(days=10)).strftime("%Y-%m-%d")
        date_plus_10 = (start_dt + timedelta(days=10)).strftime("%Y-%m-%d")

    return {
        "disaster_id": event_id,
        "disaster_type": first_value(row, ("disaster_type", "Disaster Type")),
        "country": first_value(row, ("country", "Country")),
        "region": first_value(row, ("region", "Region")),
        "subregion": first_value(row, ("subregion", "Subregion")),
        "event_name": first_value(row, ("Event Name", "event_name")),
        "emdat_location": first_value(row, ("Location", "emdat_location")),
        "position_source": first_value(row, ("position_source",)),
        "latitude": first_value(row, ("latitude", "Latitude", "final_latitude")),
        "longitude": first_value(row, ("longitude", "Longitude", "final_longitude")),
        "start_date": start_date,
        "date_minus_10": date_minus_10,
        "date_plus_10": date_plus_10,
        "weather_data": None,
        "satellite_data_path": None,
        "location_context": location_context_from_row(row),
        "news_data": None,
        "news_data_searched": False,
        "news_retrieval_status": "pending",
    }


# Checks whether a record already has news from the current news engine version.
def has_current_news(record: Mapping[str, Any], news_engine_version: str) -> bool:
    metadata = (record.get("news_data") or {}).get("search_metadata") or {}
    return (
        bool(record.get("news_data_searched"))
        and metadata.get("news_engine_version") == news_engine_version
    )


# Selects the input rows for this run using offset and limit.
def selected_rows(frame: pd.DataFrame, offset: int, limit: int) -> pd.DataFrame:
    selected = frame.iloc[offset:].copy() if offset else frame.copy()
    if limit:
        selected = selected.head(limit).copy()
    return selected


# Writes a compact progress CSV from the current JSON checkpoint.
def write_progress_csv(records: Mapping[str, Mapping[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for event_id, record in records.items():
        metadata = (record.get("news_data") or {}).get("search_metadata") or {}
        rows.append(
            {
                "event_id": event_id,
                "start_date": record.get("start_date", ""),
                "country": record.get("country", ""),
                "disaster_type": record.get("disaster_type", ""),
                "position_source": record.get("position_source", ""),
                "latitude": record.get("latitude", ""),
                "longitude": record.get("longitude", ""),
                "news_data_searched": record.get("news_data_searched", False),
                "news_retrieval_status": record.get("news_retrieval_status", ""),
                "total_articles_retrieved": metadata.get("total_articles_retrieved", 0),
                "sources_successfully_resolved": " | ".join(
                    metadata.get("sources_successfully_resolved") or []
                ),
                "news_engine_version": metadata.get("news_engine_version", ""),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


# Returns the minimal fields required to call the news engine.
def missing_required_fields(record: Mapping[str, Any]) -> List[str]:
    required = ["disaster_type", "country", "start_date"]
    return [field for field in required if not clean_text(record.get(field))]


# Defines the command-line options for the 2014+ news retrieval batch.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a news dataset from the final geocoded EM-DAT 2014+ CSV."
    )
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--progress-csv", default=str(DEFAULT_PROGRESS_CSV))
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--sleep-seconds", type=float, default=3.0)
    parser.add_argument("--force-news", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


# Runs the resumable news retrieval batch and updates the JSON checkpoint.
def main() -> None:
    args = parse_args()
    get_news_sources = None
    news_engine_version = ""
    if not args.dry_run:
        from support.news_engine import NEWS_ENGINE_VERSION, get_all_news_sources

        get_news_sources = get_all_news_sources
        news_engine_version = NEWS_ENGINE_VERSION

    input_csv = Path(args.input_csv)
    output_json = Path(args.output_json)
    progress_csv = Path(args.progress_csv)

    frame = read_events(input_csv)
    id_column = event_id_column(frame)
    selected = selected_rows(frame, args.offset, args.limit)
    records: Dict[str, Dict[str, Any]] = load_checkpoint(output_json)

    print(f"Input rows: {len(frame)}")
    print(f"Selected rows: {len(selected)}")
    print(f"Existing records: {len(records)}")
    print(f"Dry run: {args.dry_run}")
    print(f"Output JSON: {output_json}")
    print(f"Progress CSV: {progress_csv}")

    processed = 0
    skipped_existing = 0
    skipped_missing = 0
    with_news = 0

    for position, (_, row) in enumerate(selected.iterrows(), start=1):
        event_id = first_value(row, (id_column,))
        if not event_id:
            skipped_missing += 1
            continue

        record = record_from_row(row, event_id)
        existing = records.get(event_id)
        if existing:
            record.update(existing)

        missing = missing_required_fields(record)
        if missing:
            record["news_retrieval_status"] = "skipped_missing_required"
            record["news_retrieval_error"] = ",".join(missing)
            records[event_id] = record
            skipped_missing += 1
            continue

        if not args.dry_run and not args.force_news and has_current_news(record, news_engine_version):
            skipped_existing += 1
            continue

        print(
            f"[{position}/{len(selected)}] {event_id}: "
            f"{record['disaster_type']} - {record['country']} - {record['start_date']}",
            flush=True,
        )

        if args.dry_run:
            record["news_retrieval_status"] = "dry_run"
            records[event_id] = record
            processed += 1
            continue

        try:
            if get_news_sources is None:
                raise RuntimeError("news engine is not available")
            news_payload = get_news_sources(
                disaster_type=record["disaster_type"],
                country=record["country"],
                start_date=record["start_date"],
                region=record.get("region", "Global"),
                lat=record.get("latitude", ""),
                lon=record.get("longitude", ""),
                location_context=record.get("location_context") or {},
                reliefweb_appname=RELIEFWEB_APPNAME,
            )
            record["news_data"] = news_payload
            record["news_data_searched"] = True
            record["news_retrieval_status"] = "searched"
            article_count = int(
                news_payload.get("search_metadata", {}).get("total_articles_retrieved") or 0
            )
            if article_count:
                with_news += 1
        except Exception as exc:
            record["news_data_searched"] = False
            record["news_retrieval_status"] = "error"
            record["news_retrieval_error"] = f"{type(exc).__name__}: {exc}"

        records[event_id] = record
        processed += 1
        save_checkpoint(records, output_json)

        if args.sleep_seconds > 0:
            import time

            time.sleep(args.sleep_seconds)

    if not args.dry_run:
        save_checkpoint(records, output_json)
    write_progress_csv(records, progress_csv)

    print()
    print(f"Processed this run: {processed}")
    print(f"Skipped existing current-version news: {skipped_existing}")
    print(f"Skipped missing required fields: {skipped_missing}")
    print(f"Events with news this run: {with_news}")
    print(f"Total records in output JSON: {len(records)}")


if __name__ == "__main__":
    main()
