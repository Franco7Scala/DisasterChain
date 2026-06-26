import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
import requests

from support.constants import (
    NOMINATIM_SEARCH_URL,
    NOMINATIM_USER_AGENT,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_QUERIES_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_RESULTS_CSV,
    TEXT_GEOCODING_DEFAULT_LIMIT,
    TEXT_GEOCODING_DEFAULT_SLEEP_SECONDS,
)


QUERY_ID_COLUMN = "geocoding_query_id"
RESULT_COLUMNS = [
    "geocoding_query_id",
    "query_rank",
    "geocoding_query",
    "geocoding_query_key",
    "query_quality",
    "event_count",
    "candidate_row_count",
    "provider",
    "requested_at_utc",
    "geocoding_status",
    "latitude",
    "longitude",
    "display_name",
    "importance",
    "osm_type",
    "osm_id",
    "place_class",
    "place_type",
    "bbox_south",
    "bbox_north",
    "bbox_west",
    "bbox_east",
    "raw_result_json",
    "error",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_existing_results(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return pd.read_csv(path)


def completed_query_ids(results: pd.DataFrame) -> Set[str]:
    if results.empty or QUERY_ID_COLUMN not in results.columns:
        return set()
    done = results["geocoding_status"].isin(["matched", "no_result"])
    return set(results.loc[done, QUERY_ID_COLUMN].dropna().astype(str))


def selected_queries(
    queries: pd.DataFrame,
    existing_results: pd.DataFrame,
    limit: Optional[int],
    min_rank: Optional[int],
    max_rank: Optional[int],
    only_quality: Set[str],
    force: bool,
) -> pd.DataFrame:
    selected = queries.copy()
    if min_rank is not None:
        selected = selected[selected["query_rank"].astype(int).ge(min_rank)]
    if max_rank is not None:
        selected = selected[selected["query_rank"].astype(int).le(max_rank)]
    if only_quality:
        selected = selected[selected["query_quality"].isin(only_quality)]
    if not force:
        selected = selected[~selected[QUERY_ID_COLUMN].astype(str).isin(completed_query_ids(existing_results))]
    if limit is not None and limit > 0:
        selected = selected.head(limit)
    return selected.reset_index(drop=True)


def csv_set(value: Optional[str]) -> Set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def parse_bbox(value) -> Dict[str, str]:
    if not isinstance(value, list) or len(value) != 4:
        return {"bbox_south": "", "bbox_north": "", "bbox_west": "", "bbox_east": ""}
    return {
        "bbox_south": value[0],
        "bbox_north": value[1],
        "bbox_west": value[2],
        "bbox_east": value[3],
    }


def base_result(row: pd.Series, status: str, error: str = "") -> Dict:
    return {
        "geocoding_query_id": row.get("geocoding_query_id", ""),
        "query_rank": row.get("query_rank", ""),
        "geocoding_query": row.get("geocoding_query", ""),
        "geocoding_query_key": row.get("geocoding_query_key", ""),
        "query_quality": row.get("query_quality", ""),
        "event_count": row.get("event_count", ""),
        "candidate_row_count": row.get("candidate_row_count", ""),
        "provider": "nominatim",
        "requested_at_utc": utc_now(),
        "geocoding_status": status,
        "latitude": "",
        "longitude": "",
        "display_name": "",
        "importance": "",
        "osm_type": "",
        "osm_id": "",
        "place_class": "",
        "place_type": "",
        "bbox_south": "",
        "bbox_north": "",
        "bbox_west": "",
        "bbox_east": "",
        "raw_result_json": "",
        "error": error,
    }


def result_from_response(row: pd.Series, payload: List[Dict]) -> Dict:
    if not payload:
        return base_result(row, "no_result")

    first = payload[0]
    result = base_result(row, "matched")
    result.update(
        {
            "latitude": first.get("lat", ""),
            "longitude": first.get("lon", ""),
            "display_name": first.get("display_name", ""),
            "importance": first.get("importance", ""),
            "osm_type": first.get("osm_type", ""),
            "osm_id": first.get("osm_id", ""),
            "place_class": first.get("class", ""),
            "place_type": first.get("type", ""),
            "raw_result_json": json.dumps(first, ensure_ascii=False),
        }
    )
    result.update(parse_bbox(first.get("boundingbox")))
    return result


def geocode_query(
    session: requests.Session,
    row: pd.Series,
    endpoint_url: str,
    timeout_seconds: int,
) -> Dict:
    params = {
        "q": row["geocoding_query"],
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 1,
    }
    try:
        response = session.get(endpoint_url, params=params, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return base_result(row, "error", "unexpected response format")
        return result_from_response(row, payload)
    except (requests.RequestException, ValueError) as exc:
        return base_result(row, "error", str(exc))


def write_results(path: Path, rows: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=RESULT_COLUMNS).to_csv(path, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Geocode a small batch of prepared text queries. Results are cached "
            "in the output CSV and already matched/no-result queries are skipped "
            "unless --force is used."
        )
    )
    parser.add_argument("--queries-csv", default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_QUERIES_CSV)
    parser.add_argument("--results-csv", default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_RESULTS_CSV)
    parser.add_argument("--endpoint-url", default=NOMINATIM_SEARCH_URL)
    parser.add_argument("--limit", type=int, default=TEXT_GEOCODING_DEFAULT_LIMIT)
    parser.add_argument("--min-rank", type=int)
    parser.add_argument("--max-rank", type=int)
    parser.add_argument(
        "--only-quality",
        default="usable",
        help="Comma-separated query_quality values to geocode. Default: usable.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=TEXT_GEOCODING_DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries_path = Path(args.queries_csv)
    results_path = Path(args.results_csv)

    if not queries_path.exists():
        raise SystemExit(
            "Text geocoding queries CSV not found.\n"
            f"Expected path: {queries_path}\n\n"
            "Run prepare_text_geocoding_candidates.py --natural-only first."
        )

    queries = pd.read_csv(queries_path)
    missing = {QUERY_ID_COLUMN, "query_rank", "geocoding_query", "query_quality"} - set(queries.columns)
    if missing:
        raise SystemExit("Queries CSV missing column(s): " + ", ".join(sorted(missing)))

    existing_results = load_existing_results(results_path)
    batch = selected_queries(
        queries=queries,
        existing_results=existing_results,
        limit=args.limit,
        min_rank=args.min_rank,
        max_rank=args.max_rank,
        only_quality=csv_set(args.only_quality),
        force=args.force,
    )

    print(f"Queries input: {len(queries)}")
    print(f"Existing results: {len(existing_results)}")
    print(f"Selected for this run: {len(batch)}")
    print(f"Results CSV: {results_path}")

    if args.dry_run:
        print()
        print("Dry run. No external geocoding requests will be made.")
        if len(batch):
            print(batch[["query_rank", "geocoding_query", "query_quality", "event_count"]].to_string(index=False))
        return

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": NOMINATIM_USER_AGENT,
            "Accept": "application/json",
        }
    )

    output_rows = existing_results.to_dict("records") if len(existing_results) else []
    matched = 0
    no_result = 0
    errors = 0
    for index, (_, row) in enumerate(batch.iterrows(), start=1):
        print(f"[{index}/{len(batch)}] {row['geocoding_query']}")
        result = geocode_query(
            session=session,
            row=row,
            endpoint_url=args.endpoint_url,
            timeout_seconds=args.timeout_seconds,
        )
        output_rows.append(result)
        write_results(results_path, output_rows)

        status = result["geocoding_status"]
        if status == "matched":
            matched += 1
        elif status == "no_result":
            no_result += 1
        else:
            errors += 1
        if index < len(batch):
            time.sleep(args.sleep_seconds)

    print()
    print("Geocoding run completed.")
    print(f"matched: {matched}")
    print(f"no_result: {no_result}")
    print(f"error: {errors}")
    print(f"Results CSV: {results_path}")


if __name__ == "__main__":
    main()
