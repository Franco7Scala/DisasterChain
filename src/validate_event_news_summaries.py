from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import pandas as pd

from support.event_news_summary import (
    INSUFFICIENT_INFORMATION,
    clean_text,
    summary_looks_unsupported,
)


DEFAULT_INPUT_CSV = Path(
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5.csv"
)
DEFAULT_OUTPUT_CSV = Path(
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5_validated.csv"
)
DEFAULT_INPUT_JSONL = Path(
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5.jsonl"
)
DEFAULT_OUTPUT_JSONL = Path(
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5_validated.jsonl"
)
DEFAULT_INPUT_COVERAGE_CSV = Path(
    "results/news_reasoning/event_news_summaries_coverage_2014_plus_llm70b_v5.csv"
)
DEFAULT_OUTPUT_COVERAGE_CSV = Path(
    "results/news_reasoning/event_news_summaries_coverage_2014_plus_llm70b_v5_validated.csv"
)


# Defines the command-line options for validating already generated summaries.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate generated event summaries without calling the LLM again."
    )
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--input-jsonl", default=str(DEFAULT_INPUT_JSONL))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL))
    parser.add_argument("--input-coverage-csv", default=str(DEFAULT_INPUT_COVERAGE_CSV))
    parser.add_argument("--output-coverage-csv", default=str(DEFAULT_OUTPUT_COVERAGE_CSV))
    return parser.parse_args()


# Returns the cleaned summary and the validation status for one row.
def validate_summary_row(row: Dict[str, Any]) -> Tuple[str, str]:
    summary = clean_text(row.get("event_summary"))
    call_status = clean_text(row.get("llm_call_status"))
    existing_status = clean_text(row.get("summary_validation_status"))

    if summary_looks_unsupported(summary):
        return INSUFFICIENT_INFORMATION, "rejected_unrelated_news_admission"

    if summary == INSUFFICIENT_INFORMATION:
        if call_status == "skipped_insufficient_news":
            return summary, "not_called_insufficient_news"
        if existing_status and existing_status != "pending":
            return summary, existing_status
        return summary, "insufficient_information_from_llm"

    if not summary:
        return INSUFFICIENT_INFORMATION, "empty_summary"

    if existing_status and existing_status != "pending":
        return summary, existing_status
    return summary, "accepted"


# Applies the summary validation rule to a DataFrame.
def validate_summary_frame(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    summaries = []
    statuses = []
    for row in output.to_dict("records"):
        summary, status = validate_summary_row(row)
        summaries.append(summary)
        statuses.append(status)
    output["event_summary"] = summaries
    output["summary_validation_status"] = statuses
    return output


# Reads JSONL rows while skipping blank lines.
def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                yield payload


# Writes JSONL rows with stable UTF-8 encoding.
def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# Validates a JSONL file when it is available.
def validate_jsonl(input_path: Path, output_path: Path) -> int:
    if not input_path.exists():
        return 0

    output_rows = []
    for row in read_jsonl(input_path):
        summary, status = validate_summary_row(row)
        row["event_summary"] = summary
        row["summary_validation_status"] = status
        output_rows.append(row)
    write_jsonl(output_path, output_rows)
    return len(output_rows)


# Adds validation status to the compact coverage CSV.
def validate_coverage(
    input_path: Path,
    output_path: Path,
    summary_frame: pd.DataFrame,
) -> int:
    if not input_path.exists():
        return 0

    coverage = pd.read_csv(input_path)
    status_by_event = summary_frame.set_index("event_id")["summary_validation_status"]
    coverage["summary_validation_status"] = coverage["event_id"].map(status_by_event)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(output_path, index=False)
    return len(coverage)


# Runs validation and writes the cleaned artifacts.
def main() -> None:
    args = parse_args()

    input_csv = Path(args.input_csv)
    output_csv = Path(args.output_csv)
    input_jsonl = Path(args.input_jsonl)
    output_jsonl = Path(args.output_jsonl)
    input_coverage_csv = Path(args.input_coverage_csv)
    output_coverage_csv = Path(args.output_coverage_csv)

    frame = pd.read_csv(input_csv)
    validated = validate_summary_frame(frame)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    validated.to_csv(output_csv, index=False)
    jsonl_rows = validate_jsonl(input_jsonl, output_jsonl)
    coverage_rows = validate_coverage(input_coverage_csv, output_coverage_csv, validated)

    print("rows:", len(validated))
    print()
    print("summary_validation_status:")
    print(validated["summary_validation_status"].value_counts(dropna=False).to_string())
    print()
    print(
        "usable summaries:",
        int(validated["event_summary"].fillna("").ne(INSUFFICIENT_INFORMATION).sum()),
    )
    print(
        "INSUFFICIENT_INFORMATION:",
        int(validated["event_summary"].fillna("").eq(INSUFFICIENT_INFORMATION).sum()),
    )
    print()
    print("Output CSV:", output_csv)
    if jsonl_rows:
        print("Output JSONL:", output_jsonl)
    if coverage_rows:
        print("Output coverage CSV:", output_coverage_csv)


if __name__ == "__main__":
    main()
