from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from generate_event_news_summaries import (
    DEFAULT_EVENT_CSV,
    DEFAULT_OUTPUT_DIR,
    enrich_jsonl_rows,
    existing_event_ids,
    output_metadata,
    read_dataset,
    read_event_rows,
    read_jsonl_rows,
    selected_records,
    write_jsonl_row,
)
from support.event_news_summary import (
    CAUSAL_CHAIN_PROMPT_VERSION,
    INSUFFICIENT_INFORMATION,
    build_causal_chain_prompt,
    build_event_news_context,
    clean_text,
    extract_causal_chain_from_news,
)
from support.reasoner import Reasoner


DEFAULT_INPUT_JSON = (
    DEFAULT_OUTPUT_DIR / "final_environmental_causal_dataset_2014_plus_news.json"
)
DEFAULT_CAUSAL_CHAIN_MODEL = "Qwen/Qwen2.5-72B-Instruct"
DEFAULT_SUMMARY_CSV = (
    DEFAULT_OUTPUT_DIR / "event_news_summaries_2014_plus_llm70b_v5_validated.csv"
)
DEFAULT_OUTPUT_JSONL = DEFAULT_OUTPUT_DIR / "event_causal_chains.jsonl"
DEFAULT_OUTPUT_CSV = DEFAULT_OUTPUT_DIR / "event_causal_chains.csv"
DEFAULT_COVERAGE_CSV = DEFAULT_OUTPUT_DIR / "event_causal_chains_coverage.csv"
EMPTY_CAUSAL_CHAIN_JSON = json.dumps({"causal_chain": []}, ensure_ascii=False)
COVERAGE_COLUMNS = [
    "event_id",
    "country",
    "disaster_type",
    "start_date",
    "location",
    "position_source",
    "latitude",
    "longitude",
    "summary_validation_status",
    "news_count",
    "causal_relevant_news_count",
    "news_rejected_for_causal_chain",
    "selected_news_count",
    "usable_news_count",
    "news_sources_count",
    "news_total_chars",
    "news_input_quality",
    "llm_call_status",
    "causal_chain_length",
    "causal_chain_parse_status",
    "causal_chain_prompt_version",
]


# Converts one causal-chain row into a CSV-friendly shape.
def csv_ready_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    output = dict(row)
    chain = output.get("causal_chain")
    if isinstance(chain, list):
        output["causal_chain_json"] = json.dumps({"causal_chain": chain}, ensure_ascii=False)
    elif "causal_chain_json" not in output:
        output["causal_chain_json"] = EMPTY_CAUSAL_CHAIN_JSON
    return output


# Writes the causal-chain CSV and the compact coverage CSV.
def write_csv_outputs(
    *,
    output_jsonl: Path,
    output_csv: Path,
    coverage_csv: Path,
    metadata_by_event: Mapping[str, Mapping[str, str]],
) -> None:
    rows = [
        csv_ready_row(row)
        for row in enrich_jsonl_rows(read_jsonl_rows(output_jsonl), metadata_by_event)
    ]
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_frame = pd.DataFrame(rows)
    output_frame.to_csv(output_csv, index=False)

    coverage_columns = [column for column in COVERAGE_COLUMNS if column in output_frame.columns]
    coverage_frame = output_frame[coverage_columns] if coverage_columns else pd.DataFrame(columns=COVERAGE_COLUMNS)
    coverage_frame.to_csv(coverage_csv, index=False)


# Returns the standard empty causal-chain fields used when the LLM is not called.
def empty_causal_chain_fields(parse_status: str) -> Dict[str, Any]:
    return {
        "causal_chain": [],
        "causal_chain_json": EMPTY_CAUSAL_CHAIN_JSON,
        "causal_chain_length": 0,
        "causal_chain_parse_status": parse_status,
        "causal_chain_raw_response": "",
        "causal_chain_prompt_version": CAUSAL_CHAIN_PROMPT_VERSION,
    }


# Reads the validated summary status used to decide which events are safe to call.
def read_summary_status(path: Optional[Path]) -> Dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        print(f"Warning: summary CSV not found, continuing without it: {path}")
        return {}

    frame = pd.read_csv(path, dtype=object)
    if "event_id" not in frame.columns:
        print(f"Warning: summary CSV has no event_id column, ignoring: {path}")
        return {}

    statuses: Dict[str, str] = {}
    for _, row in frame.iterrows():
        event_id = clean_text(row.get("event_id"))
        if not event_id:
            continue
        status = clean_text(row.get("summary_validation_status"))
        summary = clean_text(row.get("event_summary"))
        if not status:
            status = "accepted" if summary and summary != INSUFFICIENT_INFORMATION else "not_accepted"
        statuses[event_id] = status
    return statuses


# Keeps only records whose summary was accepted in the previous phase.
def accepted_summary_records(
    records: list[tuple[str, Dict[str, Any]]],
    summary_status_by_event: Mapping[str, str],
) -> list[tuple[str, Dict[str, Any]]]:
    return [
        (event_id, record)
        for event_id, record in records
        if summary_status_by_event.get(event_id) == "accepted"
    ]


# Defines the command-line options for causal-chain generation.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate LLM causal chains from retrieved news articles."
    )
    parser.add_argument("--input-json", default=str(DEFAULT_INPUT_JSON))
    parser.add_argument("--event-csv", default=str(DEFAULT_EVENT_CSV))
    parser.add_argument("--summary-csv", default=str(DEFAULT_SUMMARY_CSV))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--coverage-csv", default=str(DEFAULT_COVERAGE_CSV))
    parser.add_argument("--model-name", default=DEFAULT_CAUSAL_CHAIN_MODEL)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-articles", type=int, default=8)
    parser.add_argument("--max-article-chars", type=int, default=1800)
    parser.add_argument("--max-context-chars", type=int, default=14000)
    parser.add_argument("--causal-chain-max-new-tokens", type=int, default=512)
    parser.add_argument("--load-in-4bit", action="store_true")
    parser.add_argument("--load-in-8bit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--require-accepted-summary",
        action="store_true",
        help="Skip LLM calls for events whose validated summary was not accepted.",
    )
    parser.add_argument(
        "--accepted-summary-only",
        action="store_true",
        help="Apply offset and limit only to events with an accepted summary.",
    )
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
    summary_csv = Path(args.summary_csv) if args.summary_csv else None
    output_jsonl = Path(args.output_jsonl)
    output_csv = Path(args.output_csv)
    coverage_csv = Path(args.coverage_csv)

    records = read_dataset(input_json)
    event_rows = read_event_rows(event_csv)
    summary_status_by_event = read_summary_status(summary_csv)
    if (args.require_accepted_summary or args.accepted_summary_only) and not summary_status_by_event:
        raise SystemExit("Accepted-summary filtering requires a readable --summary-csv")

    records_for_selection = (
        accepted_summary_records(records, summary_status_by_event)
        if args.accepted_summary_only
        else records
    )
    metadata_by_event = {
        event_id: output_metadata(record, event_rows.get(event_id))
        for event_id, record in records
    }
    selected = selected_records(records_for_selection, offset=args.offset, limit=args.limit)

    if args.force and output_jsonl.exists():
        output_jsonl.unlink()

    already_done = set() if args.force else existing_event_ids(output_jsonl)

    reasoner: Optional[Reasoner] = None
    if not args.dry_run:
        reasoner = Reasoner(
            args.model_name,
            load_in_4bit=args.load_in_4bit,
            load_in_8bit=args.load_in_8bit,
            max_new_tokens=args.causal_chain_max_new_tokens,
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
            "summary_validation_status": summary_status_by_event.get(event_id, ""),
            "news_count": context.get("news_count", 0),
            "causal_relevant_news_count": context.get("relevant_news_count", 0),
            "news_rejected_for_causal_chain": context.get("news_rejected_by_relevance_filter", 0),
            "selected_news_count": context.get("selected_news_count", 0),
            "usable_news_count": context.get("usable_news_count", 0),
            "news_sources_count": context.get("news_sources_count", 0),
            "news_total_chars": context.get("news_total_chars", 0),
            "news_input_quality": context.get("news_input_quality", ""),
        }

        print(
            f"[{position}/{total}] {event_id}: "
            f"relevant={coverage_row['causal_relevant_news_count']}/"
            f"{coverage_row['news_count']} "
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
            output_row["causal_chain_prompt"] = build_causal_chain_prompt(context)

        if args.dry_run:
            output_row.update(empty_causal_chain_fields("dry_run"))
        elif (
            args.require_accepted_summary
            and summary_status_by_event.get(event_id) != "accepted"
        ):
            output_row.update(empty_causal_chain_fields("skipped_no_accepted_summary"))
            output_row["llm_call_status"] = "skipped_no_accepted_summary"
        elif (
            context.get("news_input_quality") == "insufficient"
            and not args.call_llm_on_insufficient
        ):
            output_row.update(empty_causal_chain_fields("skipped_insufficient_news"))
            output_row["llm_call_status"] = "skipped_insufficient_news"
        else:
            if reasoner is None:
                raise RuntimeError("reasoner is required when dry_run is False")
            output_row.update(
                extract_causal_chain_from_news(
                    reasoner,
                    context,
                    max_new_tokens=args.causal_chain_max_new_tokens,
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
