import argparse
import csv
import json
from pathlib import Path
from typing import Mapping


DEFAULT_CAUSAL_CSV = (
    "results/news_reasoning/"
    "event_causal_chains_qwen72b_v8_2014_plus_type_normalized.csv"
)
ACCEPTED_CAUSAL_STATUSES = {
    "parsed",
    "parsed_with_dropped_items",
    "parsed_with_dropped_unsupported_quotes",
}


# Reads an explicit true/false option without treating the string 'false' as true.
def parse_boolean(value: str) -> bool:
    value = value.strip().casefold()
    if value not in {"true", "false"}:
        raise argparse.ArgumentTypeError("Expected true or false")
    return value == "true"


# Checks the saved validated chain without repairing or revalidating LLM evidence.
def causal_chain_status(row: Mapping[str, str]) -> str:
    if (row.get("causal_chain_parse_status") or "").strip() not in ACCEPTED_CAUSAL_STATUSES:
        return "unaccepted_parse_status"
    text = row.get("causal_chain_json") or row.get("causal_chain") or ""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "invalid_json"
    chain = payload.get("causal_chain") if isinstance(payload, dict) else payload
    if not isinstance(chain, list):
        return "invalid_structure"
    if not chain:
        return "empty_chain"
    for number, item in enumerate(chain, start=1):
        if not isinstance(item, dict):
            return "invalid_structure"
        if type(item.get("n_event")) is not int or item["n_event"] != number:
            return "invalid_structure"
        if any(not isinstance(item.get(field), str) or not item[field].strip()
               for field in ("type_event", "description", "supporting_quote")):
            return "invalid_structure"
    return "valid"


# Indexes final causal-chain eligibility by event id and rejects ambiguous inputs.
def read_causal_chain_statuses(path: str) -> dict[str, str]:
    source = Path(path)
    if not source.is_file():
        raise SystemExit(f"Causal-chain CSV not found: {source}; provide --causal-csv or choose all events explicitly")
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        id_column = next((name for name in ("event_id", "DisNo.", "emdat_disaster_id", "disaster_id")
                          if name in columns), None)
        if (id_column is None or "causal_chain_parse_status" not in columns
                or not {"causal_chain_json", "causal_chain"}.intersection(columns)):
            raise SystemExit("Causal-chain CSV requires an event id, causal_chain_parse_status and causal_chain_json (or causal_chain)")
        statuses = {}
        for row in reader:
            event_id = (row.get(id_column) or "").strip()
            if not event_id or event_id in statuses:
                raise SystemExit(f"Missing or duplicate event id in causal-chain CSV: {event_id!r}")
            statuses[event_id] = causal_chain_status(row)
    return statuses
