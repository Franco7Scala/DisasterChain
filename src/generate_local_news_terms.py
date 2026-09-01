from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import pandas as pd

from support.constants import RESULTS_DIR
from support.event_news_summary import clean_text, response_json_text
from support.reasoner import DEFAULT_REASONER_MODEL, Reasoner


LOCAL_TERMS_PROMPT_VERSION = "local_news_search_terms_v1"
DEFAULT_INPUT_JSON = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "final_environmental_causal_dataset_2014_plus_news.json"
)
DEFAULT_OUTPUT_JSON = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "local_news_search_terms_llm70b.json"
)
DEFAULT_OUTPUT_CSV = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "local_news_search_terms_llm70b.csv"
)
DEFAULT_GROUPS_CSV = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "local_news_search_term_groups_without_news.csv"
)

LOCAL_TERMS_PROMPT_TEMPLATE = """You are building a local-language news search dictionary for disaster-event retrieval.

Task:
For each country/disaster_type pair, generate short disaster keywords that local newspapers in that country would likely use for this type of event.

Rules:
1. Return ONLY raw, valid JSON. Do not include explanations, greetings, markdown, or code fences.
2. Use the main language or languages commonly used by local news outlets in the country.
3. Focus on disaster keywords, not country names, city names, dates, or full article titles.
4. Prefer common journalistic terms over technical jargon.
5. Return 3 to 8 terms per country/disaster_type pair.
6. If English is one of the country's main news languages, English terms are acceptable; otherwise prefer non-English local terms.
7. Keep each term short enough to work in Google News or DuckDuckGo queries.
8. Preserve accents and local spelling when they are normally used.

Required JSON format:
{{
  "local_terms": [
    {{
      "country": "Peru",
      "disaster_type": "Flood",
      "disaster_type_key": "flood",
      "primary_languages": ["Spanish"],
      "terms": ["inundacion", "inundaciones", "desborde", "huaico"]
    }}
  ]
}}

Country/disaster_type pairs:
{groups}
"""


# Loads the event-news dataset saved by the news retrieval pipeline.
def read_dataset(path: Path) -> List[Tuple[str, Dict[str, Any]]]:
    if not path.exists():
        raise SystemExit(f"Input JSON not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, Mapping):
        raise SystemExit("Input JSON must contain an object keyed by event id")

    records: List[Tuple[str, Dict[str, Any]]] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        event_id = clean_text(value.get("disaster_id")) or str(key)
        record = dict(value)
        record.setdefault("disaster_id", event_id)
        records.append((event_id, record))
    return records


# Converts a text label into a stable lowercase lookup key.
def lookup_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Removes parenthetical EM-DAT qualifiers from disaster types.
def disaster_type_key(disaster_type: Any) -> str:
    return lookup_key(str(disaster_type or "").split("(")[0])


# Returns the number of retrieved articles already stored for one event.
def article_count(record: Mapping[str, Any]) -> int:
    metadata = (record.get("news_data") or {}).get("search_metadata") or {}
    try:
        return int(metadata.get("total_articles_retrieved") or 0)
    except (TypeError, ValueError):
        return 0


# Groups missing-news events by country and disaster type.
def country_disaster_groups(
    records: Iterable[Tuple[str, Mapping[str, Any]]],
    *,
    only_without_news: bool,
) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for event_id, record in records:
        if only_without_news and article_count(record) > 0:
            continue

        country = clean_text(record.get("country") or record.get("Country"))
        disaster_type = clean_text(record.get("disaster_type") or record.get("Disaster Type"))
        if not country or not disaster_type:
            continue

        key = (lookup_key(country), disaster_type_key(disaster_type))
        if key not in grouped:
            grouped[key] = {
                "country": country,
                "disaster_type": disaster_type,
                "disaster_type_key": key[1],
                "event_count": 0,
                "example_event_id": event_id,
                "example_location": clean_text(
                    record.get("emdat_location")
                    or (record.get("location_context") or {}).get("emdat_location")
                    or record.get("location")
                ),
            }
        grouped[key]["event_count"] += 1

    return sorted(
        grouped.values(),
        key=lambda item: (-int(item["event_count"]), item["country"], item["disaster_type"]),
    )


# Reads an existing local-terms JSON so the script can resume safely.
def read_existing_terms(path: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        return {}

    existing: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in payload.get("local_terms") or []:
        if not isinstance(item, Mapping):
            continue
        country = clean_text(item.get("country"))
        key = clean_text(item.get("disaster_type_key")) or disaster_type_key(
            item.get("disaster_type")
        )
        if not country or not key:
            continue
        existing[(lookup_key(country), lookup_key(key))] = dict(item)
    return existing


# Formats one batch of groups for the LLM prompt.
def format_groups(groups: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for index, group in enumerate(groups, start=1):
        location = clean_text(group.get("example_location")) or "unavailable"
        lines.append(
            f'{index}. country: {group["country"]} | '
            f'disaster_type: {group["disaster_type"]} | '
            f'event_count: {group["event_count"]} | '
            f'example_location: {location}'
        )
    return "\n".join(lines)


# Builds the prompt for one LLM call.
def build_local_terms_prompt(groups: Iterable[Mapping[str, Any]]) -> str:
    return LOCAL_TERMS_PROMPT_TEMPLATE.format(groups=format_groups(groups))


# Parses and validates the JSON object returned by the LLM.
def parse_local_terms_response(response: Any) -> Tuple[List[Dict[str, Any]], str]:
    json_text = response_json_text(response)
    if not json_text:
        return [], "invalid_json"

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return [], "invalid_json"

    items = payload.get("local_terms")
    if not isinstance(items, list):
        return [], "missing_local_terms"

    rows = []
    dropped = 0
    for item in items:
        if not isinstance(item, Mapping):
            dropped += 1
            continue
        country = clean_text(item.get("country"))
        disaster_type = clean_text(item.get("disaster_type"))
        key = clean_text(item.get("disaster_type_key")) or disaster_type_key(disaster_type)
        terms = [
            clean_text(term)
            for term in item.get("terms") or []
            if clean_text(term)
        ]
        languages = [
            clean_text(language)
            for language in item.get("primary_languages") or []
            if clean_text(language)
        ]
        if not country or not disaster_type or not key or not terms:
            dropped += 1
            continue
        rows.append(
            {
                "country": country,
                "disaster_type": disaster_type,
                "disaster_type_key": lookup_key(key),
                "primary_languages": languages,
                "terms": list(dict.fromkeys(terms)),
            }
        )

    if not rows:
        return [], "empty_terms"
    if dropped:
        return rows, "parsed_with_dropped_items"
    return rows, "parsed"


# Writes both the JSON dictionary and a readable CSV copy.
def write_outputs(
    terms_by_key: Mapping[Tuple[str, str], Mapping[str, Any]],
    *,
    output_json: Path,
    output_csv: Path,
    groups_csv: Path,
    groups: Iterable[Mapping[str, Any]],
    model_name: str,
) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    sorted_terms = sorted(
        (dict(value) for value in terms_by_key.values()),
        key=lambda item: (item.get("country", ""), item.get("disaster_type_key", "")),
    )
    payload = {
        "prompt_version": LOCAL_TERMS_PROMPT_VERSION,
        "model_name": model_name,
        "local_terms": sorted_terms,
    }
    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    csv_rows = []
    for item in sorted_terms:
        csv_rows.append(
            {
                "country": item.get("country", ""),
                "disaster_type": item.get("disaster_type", ""),
                "disaster_type_key": item.get("disaster_type_key", ""),
                "primary_languages": " | ".join(item.get("primary_languages") or []),
                "terms": " | ".join(item.get("terms") or []),
            }
        )
    pd.DataFrame(csv_rows).to_csv(output_csv, index=False)
    pd.DataFrame(list(groups)).to_csv(groups_csv, index=False)


# Defines the command-line options for local search-term generation.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate local-language disaster search terms for missing-news events."
    )
    parser.add_argument("--input-json", default=str(DEFAULT_INPUT_JSON))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--groups-csv", default=str(DEFAULT_GROUPS_CSV))
    parser.add_argument("--model-name", default=DEFAULT_REASONER_MODEL)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--groups-per-call", type=int, default=20)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--only-without-news", action="store_true")
    parser.add_argument("--load-in-4bit", action="store_true")
    parser.add_argument("--load-in-8bit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


# Coordinates grouping, LLM calls, validation, and output writing.
def main() -> None:
    args = parse_args()
    input_json = Path(args.input_json)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    groups_csv = Path(args.groups_csv)

    records = read_dataset(input_json)
    groups = country_disaster_groups(
        records,
        only_without_news=args.only_without_news,
    )

    existing = {} if args.force else read_existing_terms(output_json)
    pending = [
        group
        for group in groups
        if (lookup_key(group["country"]), lookup_key(group["disaster_type_key"]))
        not in existing
    ]
    selected = pending[args.offset:] if args.offset else pending
    if args.limit:
        selected = selected[: args.limit]

    print(f"Input events: {len(records)}")
    print(f"Country/disaster groups: {len(groups)}")
    print(f"Existing term groups: {len(existing)}")
    print(f"Selected groups: {len(selected)}")
    print(f"Only without news: {args.only_without_news}")
    print(f"Dry run: {args.dry_run}")
    print(f"Output JSON: {output_json}")
    print(f"Output CSV: {output_csv}")
    print(f"Groups CSV: {groups_csv}")

    if args.dry_run:
        print()
        print("First prompt:")
        print(build_local_terms_prompt(selected[: args.groups_per_call]))
        write_outputs(
            existing,
            output_json=output_json,
            output_csv=output_csv,
            groups_csv=groups_csv,
            groups=groups,
            model_name=args.model_name,
        )
        return

    reasoner = Reasoner(
        model_name=args.model_name,
        load_in_4bit=args.load_in_4bit,
        load_in_8bit=args.load_in_8bit,
        max_new_tokens=args.max_new_tokens,
    )

    terms_by_key = dict(existing)
    processed_groups = 0
    parsed_groups = 0

    for start in range(0, len(selected), args.groups_per_call):
        batch = selected[start : start + args.groups_per_call]
        print(
            f"[{start + 1}/{len(selected)}] "
            f"groups in this LLM call: {len(batch)}",
            flush=True,
        )
        prompt = build_local_terms_prompt(batch)
        raw_response = reasoner.ask(prompt, max_new_tokens=args.max_new_tokens)
        parsed_rows, parse_status = parse_local_terms_response(raw_response)
        print(f"parse_status: {parse_status}; parsed rows: {len(parsed_rows)}", flush=True)

        for row in parsed_rows:
            key = (lookup_key(row["country"]), lookup_key(row["disaster_type_key"]))
            terms_by_key[key] = row
        processed_groups += len(batch)
        parsed_groups += len(parsed_rows)

        write_outputs(
            terms_by_key,
            output_json=output_json,
            output_csv=output_csv,
            groups_csv=groups_csv,
            groups=groups,
            model_name=args.model_name,
        )

    print()
    print(f"Processed groups: {processed_groups}")
    print(f"Parsed term groups: {parsed_groups}")
    print(f"Total term groups in output: {len(terms_by_key)}")


if __name__ == "__main__":
    main()
