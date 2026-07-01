import argparse
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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
    "matched_query",
    "query_variant",
    "result_rank",
    "result_score",
    "result_quality",
    "result_quality_reasons",
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
    "bbox_width_deg",
    "bbox_height_deg",
    "raw_result_json",
    "error",
]
ADMIN_HINT_RE = re.compile(
    r"\b(?:autonomous region|city|city area|county|department|district|"
    r"governorate|municipality|prefecture|province|prov\.|regency|region|"
    r"state|territory)\b",
    re.IGNORECASE,
)
PLACE_QUALIFIER_RE = re.compile(
    r"\b(?:autonomous region|capital city area|capital city|city area|city|"
    r"counties|county|departments|department|districts|district|"
    r"governorates|governorate|municipality|prefecture|provinces|province|"
    r"prov\.|regencies|regency|regions|region|states|state|territory|"
    r"villages|village)\b\.?",
    re.IGNORECASE,
)
ISLAND_ABBREVIATION_RE = re.compile(r"\bisl\.(?=\W|$)", re.IGNORECASE)
COMPOSITE_ADMIN_RE = re.compile(
    r"^(?P<left>.+?)\s+and\s+(?P<right>.+?)\s+"
    r"(?P<unit>counties|departments|districts|governorates|islands|"
    r"provinces|regencies|regions|states|villages)$",
    re.IGNORECASE,
)
PARENTHETICAL_RE = re.compile(r"\(([^()]+)\)")
TRAILING_PARENTHESES_RE = re.compile(r"\s*\([^()]+\)\s*")
WHITESPACE_RE = re.compile(r"\s+")
BAD_RESULT_CLASSES = {"amenity", "building", "craft", "historic", "leisure", "office", "shop", "tourism"}
GOOD_RESULT_CLASSES = {"boundary", "place"}
GOOD_RESULT_TYPES = {
    "administrative",
    "city",
    "county",
    "island",
    "municipality",
    "province",
    "region",
    "state",
    "town",
    "village",
}
REVIEW_RESULT_SCORE_THRESHOLD = 3.0
MIN_ADMIN_HINT_BBOX_SIDE_DEG = 0.75
SPLIT_COMPONENT_VARIANT = "split_component"
PLURAL_ADMIN_UNITS = {
    "departments": "department",
    "districts": "district",
    "governorates": "governorate",
    "islands": "island",
    "provinces": "province",
    "regencies": "regency",
    "regions": "region",
    "states": "state",
    "villages": "village",
}
COUNTRY_ALIASES = {
    "bolivia plurinational state of": ["Bolivia"],
    "cote d ivoire": ["Ivory Coast", "Cote d'Ivoire"],
    "iran islamic republic of": ["Iran"],
    "tanzania united republic of": ["Tanzania"],
    "venezuela bolivarian republic of": ["Venezuela"],
    "viet nam": ["Vietnam"],
    "russian federation": ["Russia"],
    "state of palestine": ["Palestine"],
    "taiwan province of china": ["Taiwan"],
    "turkiye": ["Turkey"],
}
PLACE_ALIASES = {
    "bak city area": ["Baku"],
    "kahele territory": ["Kalehe Territory", "Kalehe"],
    "north caucasus": ["North Caucasian Federal District"],
    "northern luzon": ["Luzon"],
    "northern palawan": ["Palawan"],
    "region de bruxelles capitale brussels hoofdstedelijk gewest": [
        "Brussels Capital Region",
        "Brussels-Capital Region",
    ],
    "region de bruxelles capitale brussels hoofdstedelijk gewes": [
        "Brussels Capital Region",
        "Brussels-Capital Region",
    ],
    "region wallonne": ["Wallonia"],
    "sistan and baluchistan province": [
        "Sistan and Baluchestan Province",
        "Sistan and Baluchestan",
    ],
    "sistan and baluchistan": ["Sistan and Baluchestan"],
    "sitaro islands regency": [
        "Siau Tagulandang Biaro Regency",
        "Siau Tagulandang Biaro",
        "Sitaro Islands",
    ],
    "sitaro islands": ["Siau Tagulandang Biaro", "Sitaro Islands"],
    "southern sumatra": ["South Sumatra", "Sumatra"],
    "tomasina province": ["Toamasina Province", "Toamasina"],
    "thi qar governorate": ["Dhi Qar Governorate", "Dhi Qar"],
    "sumatra north": ["North Sumatra"],
    "santa caterina state": ["Santa Catarina state", "Santa Catarina"],
    "gaza strip": ["Gaza Strip", "Gaza", "Gaza Governorate"],
    "gorizia statistical region": ["Goriska Statistical Region"],
    "east marakwet": ["Elgeyo Marakwet", "Elgeyo-Marakwet County"],
    "kinshasa capital city area": ["Kinshasa"],
    "kinshasa capital city": ["Kinshasa"],
    "lucon isl": ["Luzon Island", "Luzon"],
    "lucon island": ["Luzon Island", "Luzon"],
    "vlaams gewest": ["Flanders"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_existing_results(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return pd.read_csv(path)


def completed_query_ids(results: pd.DataFrame) -> Set[str]:
    if results.empty or QUERY_ID_COLUMN not in results.columns:
        return set()
    done = results["geocoding_status"].isin(["matched", "matched_review", "no_result"])
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


def strip_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char))


def normalize_text(value: str) -> str:
    text = strip_accents(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def compact_text(value: str) -> str:
    text = str(value or "")
    replacements = {
        "\u00e2\u20ac\u0090": "-",
        "\u00e2\u20ac\u0091": "-",
        "\u00e2\u20ac\u0092": "-",
        "\u00e2\u20ac\u0093": "-",
        "\u00e2\u20ac\u0094": "-",
        "\u00e2\u20ac\u2122": "'",
        "\u00e2\u20ac\u02dc": "'",
        "\u00c2\u00b4": "'",
        "\u00ca\u00bc": "'",
        "\u2019": "'",
        "\u2018": "'",
        "`": "'",
        "\u00b4": "'",
        "\u02bc": "'",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return WHITESPACE_RE.sub(" ", text.replace("-", " ")).strip(" ,.")


def strip_context_phrases(value: str) -> str:
    text = compact_text(value)
    text = re.sub(r"^\s*and\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*near\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*north\s+of\s+(?:the\s+)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(?:the\s+)?town\s+of\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+in\s+the\s+(?:north|south|east|west)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:area|areas)$", "", text, flags=re.IGNORECASE)
    return compact_text(text)


def strip_leading_article(value: str) -> str:
    return compact_text(re.sub(r"^\s*the\s+", "", str(value or ""), flags=re.IGNORECASE))


def parenthetical_components(place: str) -> List[str]:
    components: List[str] = []
    for match in PARENTHETICAL_RE.findall(str(place or "")):
        cleaned = strip_context_phrases(match)
        if cleaned and cleaned not in components:
            components.append(cleaned)
    return components


def without_parentheses(place: str) -> str:
    return strip_context_phrases(TRAILING_PARENTHESES_RE.sub(" ", str(place or "")))


def add_unique(values: List[str], value: str) -> None:
    value = strip_context_phrases(value)
    if value and value not in values:
        values.append(value)


def split_place_list(value: str) -> List[str]:
    cleaned = without_parentheses(value)
    chunks = [
        strip_leading_article(part)
        for part in re.split(r"\s*,\s*|\s+\band\b\s+", cleaned)
        if strip_leading_article(part)
    ]
    if 1 < len(chunks) <= 5 and all(1 <= len(part.split()) <= 5 for part in chunks):
        return chunks
    context_chunks = [
        strip_leading_article(part)
        for part in re.split(r"\s+\bin\b\s+", cleaned)
        if strip_leading_article(part)
    ]
    if (
        len(context_chunks) == 2
        and all(1 <= len(part.split()) <= 5 for part in context_chunks)
    ):
        return context_chunks
    return []


def place_core(value: str) -> str:
    return normalize_text(PLACE_QUALIFIER_RE.sub(" ", str(value or "")))


def alias_values(value: str, aliases: Dict[str, List[str]]) -> List[str]:
    return aliases.get(normalize_text(value), [])


def place_variants(place: str) -> List[str]:
    variants: List[str] = []

    def add(value: str) -> None:
        add_unique(variants, value)

    add(place)
    add(strip_context_phrases(place))
    add(without_parentheses(place))
    expanded_island = compact_text(ISLAND_ABBREVIATION_RE.sub("Island", place))
    add(expanded_island)
    for part in re.split(r"\s*/\s*", place):
        add(part)
    for variant in list(variants):
        for alias in alias_values(variant, PLACE_ALIASES):
            add(alias)
    return variants


def country_variants(country: str) -> List[str]:
    variants: List[str] = []

    def add(value: str) -> None:
        value = compact_text(value)
        if value and value not in variants:
            variants.append(value)

    add(country)
    for alias in alias_values(country, COUNTRY_ALIASES):
        add(alias)
    return variants


def composite_place_components(place: str) -> List[str]:
    original_place = compact_text(place)
    place = without_parentheses(original_place)
    components: List[str] = []

    def add(value: str) -> None:
        add_unique(components, value)
        for alias in alias_values(value, PLACE_ALIASES):
            add_unique(components, alias)

    match = COMPOSITE_ADMIN_RE.match(place)
    if match:
        unit = PLURAL_ADMIN_UNITS.get(match.group("unit").lower(), "")
        add(f"{match.group('left')} {unit}")
        add(f"{match.group('right')} {unit}")
        add(PLACE_QUALIFIER_RE.sub(" ", f"{match.group('left')} {unit}"))
        add(PLACE_QUALIFIER_RE.sub(" ", f"{match.group('right')} {unit}"))

    for component in split_place_list(place):
        add(component)
        add(PLACE_QUALIFIER_RE.sub(" ", component))

    for component in parenthetical_components(original_place):
        add(component)
        add(PLACE_QUALIFIER_RE.sub(" ", component))

    return components


def query_variants(row: pd.Series) -> List[Tuple[str, str]]:
    original_query = str(row.get("geocoding_query") or "").strip()
    place = compact_text(row.get("place_name") or "")
    country = compact_text(row.get("country") or "")

    variants: List[Tuple[str, str]] = []

    def add(label: str, query: str) -> None:
        query = compact_text(query)
        existing_queries = [existing for _, existing in variants]
        if query and query not in existing_queries:
            variants.append((label, query))

    add("original", original_query)
    if place and country:
        for place_variant in place_variants(place):
            for country_variant in country_variants(country):
                add("place_country", f"{place_variant}, {country_variant}")
                without_qualifier = compact_text(
                    PLACE_QUALIFIER_RE.sub(" ", place_variant)
                )
                if without_qualifier and without_qualifier != place_variant:
                    add(
                        "without_admin_qualifier",
                        f"{without_qualifier}, {country_variant}",
                    )
        for component in composite_place_components(place):
            for country_variant in country_variants(country):
                add(SPLIT_COMPONENT_VARIANT, f"{component}, {country_variant}")
    return variants


def bbox_side_lengths(candidate: Dict) -> Tuple[float, float]:
    bbox = candidate.get("boundingbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return 0.0, 0.0
    try:
        south, north, west, east = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return 0.0, 0.0
    return abs(east - west), abs(north - south)


def candidate_score(candidate: Dict, row: pd.Series, result_rank: int) -> Tuple[float, str, str]:
    place = normalize_text(row.get("place_name") or row.get("geocoding_query") or "")
    core_place = place_core(row.get("place_name") or row.get("geocoding_query") or "")
    display_name = normalize_text(candidate.get("display_name", ""))
    place_class = str(candidate.get("class") or "")
    place_type = str(candidate.get("type") or "")
    query = str(row.get("geocoding_query") or "")
    bbox_width_deg, bbox_height_deg = bbox_side_lengths(candidate)
    is_admin_query = bool(ADMIN_HINT_RE.search(query))

    score = 0.0
    reasons = []
    if place_class in GOOD_RESULT_CLASSES:
        score += 3.0
        reasons.append("good_class")
    if place_type in GOOD_RESULT_TYPES:
        score += 2.0
        reasons.append("good_type")
    if place and place in display_name:
        score += 2.0
        reasons.append("place_in_display_name")
    elif core_place and core_place in display_name:
        score += 2.0
        reasons.append("core_place_in_display_name")
    if result_rank == 1:
        score += 0.5
        reasons.append("first_result")
    try:
        importance = float(candidate.get("importance") or 0)
    except (TypeError, ValueError):
        importance = 0.0
    score += min(importance, 1.0)

    if place_class in BAD_RESULT_CLASSES:
        score -= 4.0
        reasons.append("poi_or_non_admin_class")
    if is_admin_query and place_class and place_class not in GOOD_RESULT_CLASSES:
        score -= 3.0
        reasons.append("admin_query_non_admin_result")
    if (
        is_admin_query
        and max(bbox_width_deg, bbox_height_deg) > 0
        and max(bbox_width_deg, bbox_height_deg) < MIN_ADMIN_HINT_BBOX_SIDE_DEG
    ):
        score -= 3.0
        reasons.append("small_bbox_for_admin_query")

    quality = "accepted" if score >= REVIEW_RESULT_SCORE_THRESHOLD else "review"
    return score, quality, ",".join(reasons)


def parse_bbox(value) -> Dict[str, str]:
    if not isinstance(value, list) or len(value) != 4:
        return {
            "bbox_south": "",
            "bbox_north": "",
            "bbox_west": "",
            "bbox_east": "",
            "bbox_width_deg": "",
            "bbox_height_deg": "",
        }
    width_deg, height_deg = bbox_side_lengths({"boundingbox": value})
    return {
        "bbox_south": value[0],
        "bbox_north": value[1],
        "bbox_west": value[2],
        "bbox_east": value[3],
        "bbox_width_deg": round(width_deg, 6),
        "bbox_height_deg": round(height_deg, 6),
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
        "matched_query": "",
        "query_variant": "",
        "result_rank": "",
        "result_score": "",
        "result_quality": "",
        "result_quality_reasons": "",
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
        "bbox_width_deg": "",
        "bbox_height_deg": "",
        "raw_result_json": "",
        "error": error,
    }


def result_from_candidate(
    row: pd.Series,
    candidate: Dict,
    matched_query: str,
    query_variant: str,
    result_rank: int,
    score: float,
    quality: str,
    quality_reasons: str,
) -> Dict:
    status = "matched" if quality == "accepted" else "matched_review"
    result = base_result(row, status)
    result.update(
        {
            "matched_query": matched_query,
            "query_variant": query_variant,
            "result_rank": result_rank,
            "result_score": round(score, 4),
            "result_quality": quality,
            "result_quality_reasons": quality_reasons,
            "latitude": candidate.get("lat", ""),
            "longitude": candidate.get("lon", ""),
            "display_name": candidate.get("display_name", ""),
            "importance": candidate.get("importance", ""),
            "osm_type": candidate.get("osm_type", ""),
            "osm_id": candidate.get("osm_id", ""),
            "place_class": candidate.get("class", ""),
            "place_type": candidate.get("type", ""),
            "raw_result_json": json.dumps(candidate, ensure_ascii=False),
        }
    )
    result.update(parse_bbox(candidate.get("boundingbox")))
    return result


def result_from_response(
    row: pd.Series,
    payload: List[Dict],
    matched_query: str,
    query_variant: str,
) -> Dict:
    if not payload:
        return base_result(row, "no_result")

    scored = []
    for result_rank, candidate in enumerate(payload, start=1):
        score, quality, quality_reasons = candidate_score(candidate, row, result_rank)
        scored.append((score, quality, quality_reasons, result_rank, candidate))

    score, quality, quality_reasons, result_rank, candidate = sorted(
        scored,
        key=lambda item: item[0],
        reverse=True,
    )[0]
    if query_variant == SPLIT_COMPONENT_VARIANT and quality == "accepted":
        quality = "review"
        quality_reasons = ",".join(
            reason for reason in [quality_reasons, "split_component_query"] if reason
        )
    return result_from_candidate(
        row=row,
        candidate=candidate,
        matched_query=matched_query,
        query_variant=query_variant,
        result_rank=result_rank,
        score=score,
        quality=quality,
        quality_reasons=quality_reasons,
    )


def geocode_query(
    session: requests.Session,
    row: pd.Series,
    endpoint_url: str,
    timeout_seconds: int,
    result_limit: int,
    variant_sleep_seconds: float,
) -> Dict:
    best_review = None
    variants = query_variants(row)
    for index, (variant_label, query) in enumerate(variants):
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": result_limit,
            "addressdetails": 1,
            "accept-language": "en",
        }
        try:
            response = session.get(endpoint_url, params=params, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                return base_result(row, "error", "unexpected response format")
            result = result_from_response(
                row,
                payload,
                matched_query=query,
                query_variant=variant_label,
            )
            if result["geocoding_status"] == "matched":
                return result
            if result["geocoding_status"] == "matched_review":
                best_review = result
        except (requests.RequestException, ValueError) as exc:
            return base_result(row, "error", str(exc))

        if index < len(variants) - 1:
            time.sleep(variant_sleep_seconds)

    return best_review or base_result(row, "no_result")


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
    parser.add_argument("--variant-sleep-seconds", type=float, default=TEXT_GEOCODING_DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--result-limit", type=int, default=5)
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
            "Accept-Language": "en",
        }
    )

    if args.force and len(existing_results) and len(batch):
        selected_ids = set(batch[QUERY_ID_COLUMN].dropna().astype(str))
        retained_results = existing_results[
            ~existing_results[QUERY_ID_COLUMN].astype(str).isin(selected_ids)
        ].copy()
    else:
        retained_results = existing_results

    output_rows = retained_results.to_dict("records") if len(retained_results) else []
    matched = 0
    matched_review = 0
    no_result = 0
    errors = 0
    for index, (_, row) in enumerate(batch.iterrows(), start=1):
        print(f"[{index}/{len(batch)}] {row['geocoding_query']}")
        result = geocode_query(
            session=session,
            row=row,
            endpoint_url=args.endpoint_url,
            timeout_seconds=args.timeout_seconds,
            result_limit=args.result_limit,
            variant_sleep_seconds=args.variant_sleep_seconds,
        )
        output_rows.append(result)
        write_results(results_path, output_rows)

        status = result["geocoding_status"]
        if status == "matched":
            matched += 1
        elif status == "matched_review":
            matched_review += 1
        elif status == "no_result":
            no_result += 1
        else:
            errors += 1
        if index < len(batch):
            time.sleep(args.sleep_seconds)

    print()
    print("Geocoding run completed.")
    print(f"matched: {matched}")
    print(f"matched_review: {matched_review}")
    print(f"no_result: {no_result}")
    print(f"error: {errors}")
    print(f"Results CSV: {results_path}")


if __name__ == "__main__":
    main()
