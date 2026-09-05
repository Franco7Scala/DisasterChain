from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import pandas as pd

from support.event_news_summary import (
    CAUSAL_CHAIN_TYPE_NORMALIZER_VERSION,
    clean_text,
    normalize_causal_event_type,
)


DEFAULT_INPUT_CSV = Path(
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus.csv"
)
DEFAULT_OUTPUT_CSV = Path(
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus_type_normalized.csv"
)
DEFAULT_INPUT_JSONL = Path(
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus.jsonl"
)
DEFAULT_OUTPUT_JSONL = Path(
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus_type_normalized.jsonl"
)
DEFAULT_TYPE_MAP_CSV = Path(
    "results/news_reasoning/event_causal_chain_type_normalization_map_qwen72b_v8_2014_plus.csv"
)
DEFAULT_SUMMARY_CSV = Path(
    "results/news_reasoning/event_causal_chain_type_normalization_summary_qwen72b_v8_2014_plus.csv"
)
EMPTY_CAUSAL_CHAIN_JSON = json.dumps({"causal_chain": []}, ensure_ascii=False)


# Defines the command-line options for causal-chain type normalization.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize type_event labels in generated causal chains."
    )
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--input-jsonl", default=str(DEFAULT_INPUT_JSONL))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL))
    parser.add_argument("--type-map-csv", default=str(DEFAULT_TYPE_MAP_CSV))
    parser.add_argument("--summary-csv", default=str(DEFAULT_SUMMARY_CSV))
    return parser.parse_args()


# Parses a causal_chain_json value while tolerating older list-style strings.
def parse_causal_chain(value: Any) -> List[Dict[str, Any]]:
    text = clean_text(value)
    if not text:
        return []

    for loader in (json.loads, ast.literal_eval):
        try:
            payload = loader(text)
        except (json.JSONDecodeError, TypeError, ValueError, SyntaxError):
            continue

        if isinstance(payload, Mapping):
            chain = payload.get("causal_chain")
        else:
            chain = payload
        if isinstance(chain, list):
            return [dict(item) for item in chain if isinstance(item, Mapping)]

    return []


# Normalizes all type_event values in one causal chain.
def normalize_causal_chain(
    causal_chain: Iterable[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, List[Tuple[str, str]]]:
    normalized_chain: List[Dict[str, Any]] = []
    changed_count = 0
    type_pairs: List[Tuple[str, str]] = []

    for item in causal_chain:
        original_type = clean_text(item.get("type_event"))
        normalized_type = normalize_causal_event_type(original_type)
        if not normalized_type:
            continue

        normalized_item = dict(item)
        normalized_item["n_event"] = len(normalized_chain) + 1
        normalized_item["type_event"] = normalized_type
        normalized_chain.append(normalized_item)
        type_pairs.append((original_type, normalized_type))
        if original_type != normalized_type:
            changed_count += 1

    return normalized_chain, changed_count, type_pairs


# Applies type normalization to every row in the causal-chain CSV.
def normalize_csv(input_csv: Path) -> Tuple[pd.DataFrame, Counter, Counter, Counter, int]:
    frame = pd.read_csv(input_csv, dtype=object).fillna("")
    output = frame.copy()
    before_counts: Counter = Counter()
    after_counts: Counter = Counter()
    pair_counts: Counter = Counter()
    events_changed = 0
    changed_steps = []

    for index, row in output.iterrows():
        chain_source = row.get("causal_chain_json") or row.get("causal_chain")
        causal_chain = parse_causal_chain(chain_source)
        normalized_chain, changed_count, type_pairs = normalize_causal_chain(causal_chain)

        for original_type, normalized_type in type_pairs:
            before_counts[original_type] += 1
            after_counts[normalized_type] += 1
            pair_counts[(original_type, normalized_type)] += 1

        if changed_count:
            events_changed += 1
        changed_steps.append(changed_count)

        output.at[index, "causal_chain_json"] = json.dumps(
            {"causal_chain": normalized_chain},
            ensure_ascii=False,
        )
        if "causal_chain" in output.columns:
            output.at[index, "causal_chain"] = json.dumps(
                normalized_chain,
                ensure_ascii=False,
            )
        output.at[index, "causal_chain_length"] = len(normalized_chain)

    output["causal_chain_type_events_changed"] = changed_steps
    output["causal_chain_type_normalizer_version"] = CAUSAL_CHAIN_TYPE_NORMALIZER_VERSION
    return output, before_counts, after_counts, pair_counts, events_changed


# Reads JSONL rows while skipping blank lines.
def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []

    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


# Writes JSONL rows with stable UTF-8 encoding.
def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


# Applies type normalization to the JSONL file when it is available.
def normalize_jsonl(input_jsonl: Path, output_jsonl: Path) -> int:
    output_rows = []
    for row in read_jsonl(input_jsonl):
        chain_source = row.get("causal_chain_json") or row.get("causal_chain")
        causal_chain = parse_causal_chain(chain_source)
        normalized_chain, changed_count, _ = normalize_causal_chain(causal_chain)

        row["causal_chain"] = normalized_chain
        row["causal_chain_json"] = json.dumps(
            {"causal_chain": normalized_chain},
            ensure_ascii=False,
        )
        row["causal_chain_length"] = len(normalized_chain)
        row["causal_chain_type_events_changed"] = changed_count
        row["causal_chain_type_normalizer_version"] = CAUSAL_CHAIN_TYPE_NORMALIZER_VERSION
        output_rows.append(row)

    if not output_rows:
        return 0
    return write_jsonl(output_jsonl, output_rows)


# Writes the before/after mapping of type_event labels.
def write_type_map(path: Path, pair_counts: Counter) -> pd.DataFrame:
    rows = [
        {
            "type_event_original": original_type,
            "type_event_normalized": normalized_type,
            "step_count": count,
            "changed": original_type != normalized_type,
        }
        for (original_type, normalized_type), count in pair_counts.items()
    ]
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(
            ["changed", "step_count", "type_event_original"],
            ascending=[False, False, True],
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return frame


# Writes a compact summary of the normalization pass.
def write_summary(
    path: Path,
    output: pd.DataFrame,
    before_counts: Counter,
    after_counts: Counter,
    events_changed: int,
) -> pd.DataFrame:
    chain_lengths = pd.to_numeric(
        output.get("causal_chain_length", pd.Series(dtype=object)),
        errors="coerce",
    ).fillna(0)
    changed_steps = pd.to_numeric(
        output.get("causal_chain_type_events_changed", pd.Series(dtype=object)),
        errors="coerce",
    ).fillna(0)
    rows = [
        ("input_rows", len(output)),
        ("events_with_non_empty_chain", int((chain_lengths > 0).sum())),
        ("causal_chain_steps", int(chain_lengths.sum())),
        ("distinct_type_events_before", len(before_counts)),
        ("distinct_type_events_after", len(after_counts)),
        ("events_with_type_event_changes", events_changed),
        ("type_event_steps_changed", int(changed_steps.sum())),
    ]
    frame = pd.DataFrame(rows, columns=["metric", "count"])
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return frame


# Runs the normalization pass and writes all derived artifacts.
def main() -> None:
    args = parse_args()

    input_csv = Path(args.input_csv)
    output_csv = Path(args.output_csv)
    input_jsonl = Path(args.input_jsonl)
    output_jsonl = Path(args.output_jsonl)
    type_map_csv = Path(args.type_map_csv)
    summary_csv = Path(args.summary_csv)

    output, before_counts, after_counts, pair_counts, events_changed = normalize_csv(input_csv)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_csv, index=False)
    jsonl_rows = normalize_jsonl(input_jsonl, output_jsonl)
    type_map = write_type_map(type_map_csv, pair_counts)
    summary = write_summary(
        summary_csv,
        output,
        before_counts,
        after_counts,
        events_changed,
    )

    print("rows:", len(output))
    print("events with non-empty chain:", int((pd.to_numeric(output["causal_chain_length"], errors="coerce").fillna(0) > 0).sum()))
    print("type labels before:", len(before_counts))
    print("type labels after:", len(after_counts))
    print("events with type changes:", events_changed)
    print("type steps changed:", int(pd.to_numeric(output["causal_chain_type_events_changed"], errors="coerce").fillna(0).sum()))
    print()
    print("Top normalization changes:")
    changed = type_map[type_map["changed"]] if not type_map.empty else pd.DataFrame()
    if changed.empty:
        print("none")
    else:
        print(changed.head(30).to_string(index=False))
    print()
    print("Summary:")
    print(summary.to_string(index=False))
    print()
    print("Output CSV:", output_csv)
    if jsonl_rows:
        print("Output JSONL:", output_jsonl)
    print("Type map CSV:", type_map_csv)
    print("Summary CSV:", summary_csv)


if __name__ == "__main__":
    main()
