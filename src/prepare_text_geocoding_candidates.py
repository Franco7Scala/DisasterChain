import argparse
import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

from support.constants import (
    RECENT_EMDAT_NATURAL_DISASTER_TYPES,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_CANDIDATES_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_QUERIES_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_SUMMARY_CSV,
    RECENT_EMDAT_REMAINING_EVENTS_CSV,
    RECENT_EMDAT_TEXT_GEOCODING_CANDIDATES_CSV,
    RECENT_EMDAT_TEXT_GEOCODING_QUERIES_CSV,
    RECENT_EMDAT_TEXT_GEOCODING_SUMMARY_CSV,
)


EVENT_ID_COLUMN = "emdat_disaster_id"
DEFAULT_TARGET_STATUSES = "location_text_only,gadm_or_admin_units"
PLACE_SEPARATOR_RE = re.compile(r"\s*(?:\||;)\s*")
WHITESPACE_RE = re.compile(r"\s+")
TRAILING_CONTEXT_RE = re.compile(r"\s+\(([^)]*)\)\s*$")
GENERIC_PLACE_RE = re.compile(
    r"^(?:various|several|multiple|many|affected|nationwide|countrywide|"
    r"unknown|not reported|not specified|na|n/a)$",
    re.IGNORECASE,
)
APPROXIMATE_PLACE_RE = re.compile(
    r"^(?:near|around|between|vicinity of|close to|surrounding)\b",
    re.IGNORECASE,
)


def has_text(value) -> bool:
    if pd.isna(value):
        return False
    text = str(value).strip()
    return bool(text and text.lower() not in {"nan", "none", "null", "[]"})


def normalize_ascii(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    return "".join(char for char in text if not unicodedata.combining(char))


def clean_text(value) -> str:
    if not has_text(value):
        return ""
    text = normalize_ascii(str(value))
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s*,\s*", ", ", text)
    return WHITESPACE_RE.sub(" ", text).strip(" ,")


def normalized_key(value: str) -> str:
    text = normalize_ascii(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def split_place_text(value) -> List[str]:
    text = clean_text(value)
    if not text:
        return []
    parts = PLACE_SEPARATOR_RE.split(text)
    result = []
    for part in parts:
        cleaned = clean_text(part)
        if not cleaned:
            continue
        match = TRAILING_CONTEXT_RE.search(cleaned)
        if match:
            context = clean_text(match.group(1))
            before = clean_text(TRAILING_CONTEXT_RE.sub("", cleaned))
            if before:
                result.append(before)
            if context:
                result.extend(split_place_text(context))
            continue
        result.append(cleaned)
    return result


def unique_places(values: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        place = clean_text(value)
        key = normalized_key(place)
        if not place or not key or key in seen:
            continue
        seen.add(key)
        result.append(place)
    return result


def place_quality(place: str) -> tuple[str, str]:
    key = normalized_key(place)
    reasons = []
    if len(key) < 3:
        reasons.append("too_short")
    if GENERIC_PLACE_RE.match(key):
        reasons.append("generic_place")
    if len(place) > 140:
        reasons.append("very_long_place_text")
    if re.search(r"\d", place):
        reasons.append("contains_number")
    if APPROXIMATE_PLACE_RE.search(place):
        reasons.append("approximate_place_text")

    if not reasons:
        return "usable", ""
    if "too_short" in reasons or "generic_place" in reasons:
        return "review", ",".join(reasons)
    return "usable_with_notes", ",".join(reasons)


def query_id(query_key: str) -> str:
    return hashlib.sha1(query_key.encode("utf-8")).hexdigest()[:12]


def target_statuses(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def csv_values(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def selected_disaster_types(args: argparse.Namespace) -> set[str]:
    selected = set(RECENT_EMDAT_NATURAL_DISASTER_TYPES) if args.natural_only else set()
    selected.update(csv_values(args.disaster_type or ""))
    return selected


def apply_disaster_type_filter(
    events: pd.DataFrame,
    disaster_types: set[str],
) -> pd.DataFrame:
    if not disaster_types:
        return events
    return events[events["disaster_type"].isin(disaster_types)].copy()


def row_places(row: pd.Series) -> tuple[List[str], str]:
    location_places = split_place_text(row.get("location"))
    if location_places:
        return unique_places(location_places), "location"

    admin_places = []
    admin_places.extend(split_place_text(row.get("gadm_admin_units_text")))
    admin_places.extend(split_place_text(row.get("admin_units_text")))
    if admin_places:
        return unique_places(admin_places), "admin_units_text"

    event_places = split_place_text(row.get("event_name"))
    if event_places:
        return unique_places(event_places), "event_name"

    return [], ""


def candidate_rows(remaining: pd.DataFrame, statuses: set[str]) -> List[Dict]:
    rows = []
    for _, row in remaining.iterrows():
        status = str(row.get("geocoding_status") or "").strip()
        if status not in statuses:
            continue

        country = clean_text(row.get("country"))
        iso = clean_text(row.get("iso")).upper()
        places, source_field = row_places(row)
        if not places or not country:
            continue

        for place_index, place in enumerate(places, start=1):
            query = f"{place}, {country}"
            key = normalized_key(query)
            quality, quality_reasons = place_quality(place)
            rows.append(
                {
                    EVENT_ID_COLUMN: row.get(EVENT_ID_COLUMN, ""),
                    "place_index": place_index,
                    "disaster_type": row.get("disaster_type", ""),
                    "disaster_subtype": row.get("disaster_subtype", ""),
                    "country": country,
                    "iso": iso,
                    "region": row.get("region", ""),
                    "start_date": row.get("start_date", ""),
                    "geocoding_status": status,
                    "text_source_field": source_field,
                    "place_name": place,
                    "geocoding_query": query,
                    "geocoding_query_key": key,
                    "geocoding_query_id": query_id(key),
                    "query_quality": quality,
                    "query_quality_reasons": quality_reasons,
                    "location": row.get("location", ""),
                    "admin_units_text": row.get("admin_units_text", ""),
                    "gadm_admin_units_text": row.get("gadm_admin_units_text", ""),
                    "event_name": row.get("event_name", ""),
                }
            )
    return rows


def build_queries(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()

    rows = []
    sort_columns = ["geocoding_query_key", "query_quality", "start_date"]
    for query_key, group in candidates.sort_values(sort_columns).groupby(
        "geocoding_query_key",
        sort=False,
    ):
        first = group.iloc[0]
        rows.append(
            {
                "geocoding_query_id": first["geocoding_query_id"],
                "geocoding_query": first["geocoding_query"],
                "geocoding_query_key": query_key,
                "country": first["country"],
                "iso": first["iso"],
                "place_name": first["place_name"],
                "event_count": group[EVENT_ID_COLUMN].nunique(),
                "candidate_row_count": len(group),
                "query_quality": first["query_quality"],
                "query_quality_reasons": first["query_quality_reasons"],
                "geocoding_statuses": " | ".join(
                    sorted(group["geocoding_status"].dropna().astype(str).unique())
                ),
                "disaster_types": " | ".join(
                    sorted(group["disaster_type"].dropna().astype(str).unique())
                ),
                "first_start_date": group["start_date"].dropna().astype(str).min(),
                "last_start_date": group["start_date"].dropna().astype(str).max(),
            }
        )

    queries = pd.DataFrame(rows)
    quality_rank = {"usable": 0, "usable_with_notes": 1, "review": 2}
    queries["_quality_rank"] = queries["query_quality"].map(quality_rank).fillna(3)
    queries = queries.sort_values(
        ["_quality_rank", "event_count", "last_start_date", "geocoding_query"],
        ascending=[True, False, False, True],
    ).drop(columns=["_quality_rank"])
    queries.insert(0, "query_rank", range(1, len(queries) + 1))
    return queries


def build_summary(
    input_remaining: pd.DataFrame,
    filtered_remaining: pd.DataFrame,
    candidates: pd.DataFrame,
    queries: pd.DataFrame,
    disaster_types: set[str],
) -> pd.DataFrame:
    rows = []

    def add(metric: str, count: int) -> None:
        rows.append({"metric": metric, "count": int(count)})

    add("remaining_input_events", input_remaining[EVENT_ID_COLUMN].nunique())
    add("remaining_events_after_type_filter", filtered_remaining[EVENT_ID_COLUMN].nunique())
    add(
        "remaining_events_filtered_out_by_type",
        input_remaining[EVENT_ID_COLUMN].nunique() - filtered_remaining[EVENT_ID_COLUMN].nunique(),
    )
    add("text_candidate_rows", len(candidates))
    add("events_with_text_candidates", candidates[EVENT_ID_COLUMN].nunique() if len(candidates) else 0)
    add("unique_geocoding_queries", len(queries))
    if disaster_types:
        add("selected_disaster_type_count", len(disaster_types))
    if candidates.empty:
        return pd.DataFrame(rows)

    for status, count in candidates["geocoding_status"].value_counts().items():
        add(f"candidate_events_{status}", candidates.loc[candidates["geocoding_status"].eq(status), EVENT_ID_COLUMN].nunique())
        add(f"candidate_rows_{status}", count)
    for disaster_type, count in candidates.groupby("disaster_type")[EVENT_ID_COLUMN].nunique().sort_values(ascending=False).items():
        add(f"candidate_events_disaster_type_{disaster_type}", count)
    for quality, count in candidates["query_quality"].value_counts().items():
        add(f"candidate_rows_quality_{quality}", count)
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare EM-DAT remaining events for a future text-geocoding step. "
            "This script only creates normalized query CSV files and does not "
            "call external geocoding services."
        )
    )
    parser.add_argument("--remaining-events-csv", default=RECENT_EMDAT_REMAINING_EVENTS_CSV)
    parser.add_argument(
        "--target-status",
        default=DEFAULT_TARGET_STATUSES,
        help=(
            "Comma-separated geocoding_status values to include. Default: "
            f"{DEFAULT_TARGET_STATUSES}."
        ),
    )
    parser.add_argument("--candidates-csv", default=RECENT_EMDAT_TEXT_GEOCODING_CANDIDATES_CSV)
    parser.add_argument("--queries-csv", default=RECENT_EMDAT_TEXT_GEOCODING_QUERIES_CSV)
    parser.add_argument("--summary-csv", default=RECENT_EMDAT_TEXT_GEOCODING_SUMMARY_CSV)
    parser.add_argument(
        "--natural-only",
        action="store_true",
        help=(
            "Keep only natural/environmental disaster types. When default output "
            "paths are used, files are written with a _natural suffix."
        ),
    )
    parser.add_argument(
        "--disaster-type",
        help=(
            "Comma-separated disaster types to include, e.g. Flood,Storm. "
            "Can be combined with --natural-only."
        ),
    )
    parser.add_argument("--limit-events", type=int, help="Optional event limit for quick checks.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    remaining_path = Path(args.remaining_events_csv)
    if not remaining_path.exists():
        raise SystemExit(
            "Remaining events CSV not found.\n"
            f"Expected path: {remaining_path}\n\n"
            "Run summarize_geocoding_coverage.py first."
        )

    remaining = pd.read_csv(remaining_path)
    required_columns = {EVENT_ID_COLUMN, "geocoding_status", "country", "iso", "disaster_type"}
    missing = required_columns - set(remaining.columns)
    if missing:
        raise SystemExit(
            "Remaining events CSV missing column(s): " + ", ".join(sorted(missing))
        )
    if args.limit_events:
        event_ids = remaining[EVENT_ID_COLUMN].dropna().astype(str).drop_duplicates().head(args.limit_events)
        remaining = remaining[remaining[EVENT_ID_COLUMN].astype(str).isin(set(event_ids))].copy()

    disaster_types = selected_disaster_types(args)
    filtered_remaining = apply_disaster_type_filter(remaining, disaster_types)
    candidates = pd.DataFrame(candidate_rows(filtered_remaining, target_statuses(args.target_status)))
    queries = build_queries(candidates)
    summary = build_summary(
        input_remaining=remaining,
        filtered_remaining=filtered_remaining,
        candidates=candidates,
        queries=queries,
        disaster_types=disaster_types,
    )

    if args.natural_only:
        candidates_path = (
            Path(args.candidates_csv)
            if args.candidates_csv != RECENT_EMDAT_TEXT_GEOCODING_CANDIDATES_CSV
            else Path(RECENT_EMDAT_NATURAL_TEXT_GEOCODING_CANDIDATES_CSV)
        )
        queries_path = (
            Path(args.queries_csv)
            if args.queries_csv != RECENT_EMDAT_TEXT_GEOCODING_QUERIES_CSV
            else Path(RECENT_EMDAT_NATURAL_TEXT_GEOCODING_QUERIES_CSV)
        )
        summary_path = (
            Path(args.summary_csv)
            if args.summary_csv != RECENT_EMDAT_TEXT_GEOCODING_SUMMARY_CSV
            else Path(RECENT_EMDAT_NATURAL_TEXT_GEOCODING_SUMMARY_CSV)
        )
    else:
        candidates_path = Path(args.candidates_csv)
        queries_path = Path(args.queries_csv)
        summary_path = Path(args.summary_csv)
    for path in [candidates_path, queries_path, summary_path]:
        path.parent.mkdir(parents=True, exist_ok=True)

    candidates.to_csv(candidates_path, index=False)
    queries.to_csv(queries_path, index=False)
    summary.to_csv(summary_path, index=False)

    print("Text geocoding preparation summary:")
    if disaster_types:
        print("Selected disaster types: " + " | ".join(sorted(disaster_types)))
    for _, row in summary.iterrows():
        print(f"{row['metric']}: {row['count']}")
    print()
    print(f"Candidate rows CSV: {candidates_path}")
    print(f"Unique queries CSV: {queries_path}")
    print(f"Summary CSV: {summary_path}")


if __name__ == "__main__":
    main()
