from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pandas as pd

from support.constants import RESULTS_DIR
from support.event_news_summary import (
    INSUFFICIENT_INFORMATION,
    build_event_news_context,
    build_summary_prompt,
    clean_text,
    merge_event_metadata,
    summarize_event_from_news,
)
from support.reasoner import DEFAULT_REASONER_MODEL, Reasoner


DEFAULT_EVENT_CSV = (
    Path(RESULTS_DIR)
    / "recent_emdat_geocoding"
    / "emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_OUTPUT_DIR = Path(RESULTS_DIR) / "news_reasoning"
DEFAULT_INPUT_JSON = DEFAULT_OUTPUT_DIR / "final_environmental_causal_dataset_2014_plus_news.json"
DEFAULT_OUTPUT_JSONL = DEFAULT_OUTPUT_DIR / "event_news_summaries.jsonl"
DEFAULT_OUTPUT_CSV = DEFAULT_OUTPUT_DIR / "event_news_summaries.csv"
DEFAULT_COVERAGE_CSV = DEFAULT_OUTPUT_DIR / "event_news_summaries_coverage.csv"
COVERAGE_COLUMNS = [
    "event_id",
    "country",
    "disaster_type",
    "start_date",
    "location",
    "position_source",
    "latitude",
    "longitude",
    "news_count",
    "selected_news_count",
    "usable_news_count",
    "news_sources_count",
    "news_total_chars",
    "news_input_quality",
    "llm_call_status",
]
OUTPUT_METADATA_FIELDS = [
    ("country", ("country", "Country")),
    ("disaster_type", ("disaster_type", "Disaster Type")),
    ("start_date", ("start_date", "Start Date", "_llm_start_date")),
    ("location", ("Location", "emdat_location", "location")),
    ("position_source", ("position_source",)),
    ("latitude", ("latitude", "Latitude", "final_latitude")),
    ("longitude", ("longitude", "Longitude", "final_longitude")),
]


# Reads the event dataset JSON in both dict and list formats.
def read_dataset(path: Path) -> List[Tuple[str, Dict[str, Any]]]:
    if not path.exists():
        raise SystemExit(f"Input dataset JSON not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    records: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            record = dict(value)
            record.setdefault("disaster_id", key)
            records.append((str(record.get("disaster_id") or key), record))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            if not isinstance(value, dict):
                continue
            event_id = str(
                value.get("disaster_id")
                or value.get("DisNo.")
                or value.get("emdat_disaster_id")
                or index
            )
            record = dict(value)
            record.setdefault("disaster_id", event_id)
            records.append((event_id, record))
    else:
        raise SystemExit("Input dataset JSON must be an object or a list")

    return records


# Reads optional event-level metadata from the final geocoding CSV.
def read_event_rows(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if path is None:
        return {}
    if not path.exists():
        print(f"Warning: event CSV not found, continuing without it: {path}")
        return {}

    frame = pd.read_csv(path, dtype=object)
    id_column = next(
        (column for column in ("DisNo.", "disaster_id", "emdat_disaster_id") if column in frame.columns),
        None,
    )
    if id_column is None:
        print(f"Warning: event CSV has no event id column, ignoring: {path}")
        return {}

    rows: Dict[str, Dict[str, Any]] = {}
    for _, row in frame.iterrows():
        event_id = str(row.get(id_column) or "").strip()
        if event_id:
            rows[event_id] = row.to_dict()
    return rows


# Extracts the small metadata block needed to audit summary outputs.
def output_metadata(
    record: Mapping[str, Any],
    event_row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    metadata = merge_event_metadata(event_row or {}, record)
    output: Dict[str, str] = {}
    for output_key, source_keys in OUTPUT_METADATA_FIELDS:
        output[output_key] = ""
        for source_key in source_keys:
            value = clean_text(metadata.get(source_key))
            if value:
                output[output_key] = value
                break
    return output


# Adds audit metadata to rows already stored in JSONL.
def enrich_jsonl_rows(
    rows: List[Dict[str, Any]],
    metadata_by_event: Mapping[str, Mapping[str, str]],
) -> List[Dict[str, Any]]:
    enriched_rows: List[Dict[str, Any]] = []
    for row in rows:
        event_id = clean_text(row.get("event_id"))
        metadata = dict(metadata_by_event.get(event_id, {}))
        enriched_rows.append({"event_id": event_id, **metadata, **row})
    return enriched_rows


# Applies offset and limit so the script can run in resumable batches.
def selected_records(
    records: List[Tuple[str, Dict[str, Any]]],
    *,
    offset: int,
    limit: int,
) -> List[Tuple[str, Dict[str, Any]]]:
    selected = records[offset:] if offset else records
    if limit:
        selected = selected[:limit]
    return selected


# Reads the JSONL output to avoid reprocessing events already completed.
def existing_event_ids(path: Path) -> set:
    if not path.exists():
        return set()
    ids = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = str(row.get("event_id") or "").strip()
            if event_id:
                ids.add(event_id)
    return ids


# Appends one processed event to the incremental JSONL output.
def write_jsonl_row(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# Loads JSONL rows back into memory for the final CSV export.
def read_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# Writes the summary CSV and the coverage CSV from the complete JSONL output.
def write_csv_outputs(
    *,
    output_jsonl: Path,
    output_csv: Path,
    coverage_csv: Path,
    metadata_by_event: Mapping[str, Mapping[str, str]],
) -> None:
    rows = enrich_jsonl_rows(read_jsonl_rows(output_jsonl), metadata_by_event)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_frame = pd.DataFrame(rows)
    output_frame.to_csv(output_csv, index=False)

    coverage_columns = [column for column in COVERAGE_COLUMNS if column in output_frame.columns]
    coverage_frame = output_frame[coverage_columns] if coverage_columns else pd.DataFrame(columns=COVERAGE_COLUMNS)
    coverage_frame.to_csv(coverage_csv, index=False)


# Defines the command-line options for summary generation.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate LLM event summaries from retrieved news articles."
    )
    parser.add_argument("--input-json", default=str(DEFAULT_INPUT_JSON))
    parser.add_argument("--event-csv", default=str(DEFAULT_EVENT_CSV))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--coverage-csv", default=str(DEFAULT_COVERAGE_CSV))
    parser.add_argument("--model-name", default=DEFAULT_REASONER_MODEL)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-articles", type=int, default=8)
    parser.add_argument("--max-article-chars", type=int, default=1800)
    parser.add_argument("--max-context-chars", type=int, default=14000)
    parser.add_argument("--summary-max-new-tokens", type=int, default=256)
    parser.add_argument("--load-in-4bit", action="store_true")
    parser.add_argument("--load-in-8bit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--include-prompts",
        action="store_true",
        help="Store full prompts in the JSONL/CSV output for audit/debugging.",
    )
    parser.add_argument(
        "--call-llm-on-insufficient",
        action="store_true",
        help="Call the LLM even when no usable news text is available.",
    )
    return parser.parse_args()


# Coordinates dataset loading, prompt building, LLM calls, and output writing.
def main() -> None:
    args = parse_args()

    input_json = Path(args.input_json)
    event_csv = Path(args.event_csv) if args.event_csv else None
    output_jsonl = Path(args.output_jsonl)
    output_csv = Path(args.output_csv)
    coverage_csv = Path(args.coverage_csv)

    records = read_dataset(input_json)
    event_rows = read_event_rows(event_csv)
    metadata_by_event = {
        event_id: output_metadata(record, event_rows.get(event_id))
        for event_id, record in records
    }
    selected = selected_records(records, offset=args.offset, limit=args.limit)

    if args.force and output_jsonl.exists():
        output_jsonl.unlink()

    already_done = set() if args.force else existing_event_ids(output_jsonl)

    reasoner: Optional[Reasoner] = None
    if not args.dry_run:
        reasoner = Reasoner(
            args.model_name,
            load_in_4bit=args.load_in_4bit,
            load_in_8bit=args.load_in_8bit,
            max_new_tokens=args.summary_max_new_tokens,
            do_sample=False,
        )

    total = len(selected)
    processed = 0
    skipped = 0

    for position, (event_id, record) in enumerate(selected, start=1):
        if event_id in already_done:
            skipped += 1
            continue

        event_row = event_rows.get(event_id)
        context = build_event_news_context(
            record,
            event_row=event_row,
            max_articles=args.max_articles,
            max_article_chars=args.max_article_chars,
            max_total_chars=args.max_context_chars,
        )
        context["event_id"] = event_id

        coverage_row = {
            "event_id": event_id,
            **output_metadata(record, event_row),
            "news_count": context.get("news_count", 0),
            "selected_news_count": context.get("selected_news_count", 0),
            "usable_news_count": context.get("usable_news_count", 0),
            "news_sources_count": context.get("news_sources_count", 0),
            "news_total_chars": context.get("news_total_chars", 0),
            "news_input_quality": context.get("news_input_quality", ""),
        }

        print(
            f"[{position}/{total}] {event_id}: "
            f"news={coverage_row['usable_news_count']} "
            f"quality={coverage_row['news_input_quality']}",
            flush=True,
        )

        output_row: Dict[str, Any] = {
            **coverage_row,
            "model_name": args.model_name,
            "llm_call_status": "dry_run" if args.dry_run else "called",
        }

        if args.include_prompts or args.dry_run:
            output_row["summary_prompt"] = build_summary_prompt(context)

        if args.dry_run:
            pass
        elif (
            context.get("news_input_quality") == "insufficient"
            and not args.call_llm_on_insufficient
        ):
            output_row.update(
                {
                    "llm_call_status": "skipped_insufficient_news",
                    "event_summary": INSUFFICIENT_INFORMATION,
                    "summary_raw_response": "",
                }
            )
        else:
            if reasoner is None:
                raise RuntimeError("reasoner is required when dry_run is False")
            output_row.update(
                summarize_event_from_news(
                    reasoner,
                    context,
                    max_new_tokens=args.summary_max_new_tokens,
                )
            )

        write_jsonl_row(output_jsonl, output_row)
        processed += 1

    write_csv_outputs(
        output_jsonl=output_jsonl,
        output_csv=output_csv,
        coverage_csv=coverage_csv,
        metadata_by_event=metadata_by_event,
    )

    print()
    print(f"Input events: {len(records)}")
    print(f"Selected events: {len(selected)}")
    print(f"Processed events this run: {processed}")
    print(f"Skipped existing events: {skipped}")
    print(f"Dry run: {args.dry_run}")
    print(f"Output JSONL: {output_jsonl}")
    print(f"Output CSV: {output_csv}")
    print(f"Coverage CSV: {coverage_csv}")


if __name__ == "__main__":
    main()
