import argparse
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd

from support.constants import (
    RECENT_EMDAT_ADM2_AOIS_CSV,
    RECENT_EMDAT_GEOCODING_CANDIDATES_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_CANDIDATES_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_EVENT_COVERAGE_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_EVENT_SUMMARY_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_RESULTS_CSV,
)


EVENT_ID_COLUMN = "emdat_disaster_id"
QUERY_ID_COLUMN = "geocoding_query_id"
TEXT_ACCEPTED_STATUS = "matched"
TEXT_REVIEW_STATUS = "matched_review"
TEXT_NO_RESULT_STATUS = "no_result"


def true_values(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def event_ids(frame: pd.DataFrame, mask: pd.Series) -> Set[str]:
    return set(frame.loc[mask, EVENT_ID_COLUMN].dropna().astype(str))


def latest_query_results(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return results.copy()
    return results.drop_duplicates(subset=[QUERY_ID_COLUMN], keep="last").copy()


def text_event_rows(
    text_candidates: pd.DataFrame,
    text_results: pd.DataFrame,
) -> pd.DataFrame:
    results = latest_query_results(text_results).rename(
        columns={"geocoding_status": "result_geocoding_status"}
    )
    merged = text_candidates.merge(
        results[
            [
                QUERY_ID_COLUMN,
                "result_geocoding_status",
                "matched_query",
                "query_variant",
                "result_score",
                "display_name",
                "latitude",
                "longitude",
                "bbox_south",
                "bbox_north",
                "bbox_west",
                "bbox_east",
                "bbox_width_deg",
                "bbox_height_deg",
            ]
        ],
        on=QUERY_ID_COLUMN,
        how="left",
        suffixes=("_candidate", "_result"),
    )

    rows: List[Dict] = []
    for event_id, group in merged.groupby(EVENT_ID_COLUMN, sort=False):
        matched_rows = group[group["result_geocoding_status"].eq(TEXT_ACCEPTED_STATUS)]
        review_rows = group[group["result_geocoding_status"].eq(TEXT_REVIEW_STATUS)]
        no_result_rows = group[group["result_geocoding_status"].eq(TEXT_NO_RESULT_STATUS)]

        if len(matched_rows):
            text_status = TEXT_ACCEPTED_STATUS
            best = best_result_row(matched_rows)
        elif len(review_rows):
            text_status = TEXT_REVIEW_STATUS
            best = best_result_row(review_rows)
        elif len(no_result_rows):
            text_status = TEXT_NO_RESULT_STATUS
            best = group.iloc[0]
        else:
            text_status = "pending"
            best = group.iloc[0]

        first = group.iloc[0]
        rows.append(
            {
                EVENT_ID_COLUMN: event_id,
                "text_event_status": text_status,
                "country": first.get("country", ""),
                "iso": first.get("iso", ""),
                "disaster_type": first.get("disaster_type", ""),
                "start_date": first.get("start_date", ""),
                "candidate_query_count": group[QUERY_ID_COLUMN].nunique(),
                "tested_query_count": group["result_geocoding_status"].notna().sum(),
                "matched_query_count": len(matched_rows),
                "matched_review_query_count": len(review_rows),
                "no_result_query_count": len(no_result_rows),
                "best_geocoding_query": best.get("geocoding_query", ""),
                "best_matched_query": best.get("matched_query", ""),
                "best_query_variant": best.get("query_variant", ""),
                "best_result_score": best.get("result_score", ""),
                "best_display_name": best.get("display_name", ""),
                "latitude": best.get("latitude", ""),
                "longitude": best.get("longitude", ""),
                "bbox_south": best.get("bbox_south", ""),
                "bbox_north": best.get("bbox_north", ""),
                "bbox_west": best.get("bbox_west", ""),
                "bbox_east": best.get("bbox_east", ""),
                "bbox_width_deg": best.get("bbox_width_deg", ""),
                "bbox_height_deg": best.get("bbox_height_deg", ""),
            }
        )
    return pd.DataFrame(rows)


def best_result_row(rows: pd.DataFrame) -> pd.Series:
    if "result_score" not in rows.columns:
        return rows.iloc[0]
    scored = rows.copy()
    scored["_score"] = pd.to_numeric(scored["result_score"], errors="coerce").fillna(-9999)
    return scored.sort_values("_score", ascending=False).iloc[0]


def base_coverage(
    candidates: pd.DataFrame,
    adm2_aois: pd.DataFrame,
) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    all_events = set(candidates[EVENT_ID_COLUMN].dropna().astype(str))
    direct_events = event_ids(candidates, true_values(candidates["has_direct_coordinates"]))
    adm2_events = event_ids(adm2_aois, adm2_aois["bbox_status"].eq("matched"))
    return all_events, direct_events, adm2_events, direct_events | adm2_events


def summary_rows(
    all_events: Set[str],
    direct_events: Set[str],
    adm2_events: Set[str],
    base_events: Set[str],
    text_events: pd.DataFrame,
) -> List[Dict]:
    total = len(all_events)
    text_matched = set(
        text_events.loc[
            text_events["text_event_status"].eq(TEXT_ACCEPTED_STATUS),
            EVENT_ID_COLUMN,
        ].astype(str)
    )
    text_review = set(
        text_events.loc[
            text_events["text_event_status"].eq(TEXT_REVIEW_STATUS),
            EVENT_ID_COLUMN,
        ].astype(str)
    )
    text_no_result = set(
        text_events.loc[
            text_events["text_event_status"].eq(TEXT_NO_RESULT_STATUS),
            EVENT_ID_COLUMN,
        ].astype(str)
    )
    text_pending = set(
        text_events.loc[
            text_events["text_event_status"].eq("pending"),
            EVENT_ID_COLUMN,
        ].astype(str)
    )
    text_any_usable = text_matched | text_review
    covered_auto = base_events | text_matched
    covered_with_review = base_events | text_any_usable

    def metric(name: str, count: int) -> Dict:
        return {
            "metric": name,
            "count": count,
            "percent_of_total": round(count / total * 100, 2) if total else 0.0,
        }

    return [
        metric("total_events", total),
        metric("direct_coordinates", len(direct_events)),
        metric("matched_adm2", len(adm2_events)),
        metric("base_geolocated_direct_or_adm2", len(base_events)),
        metric("text_candidate_events", text_events[EVENT_ID_COLUMN].nunique()),
        metric("text_matched_events", len(text_matched)),
        metric("text_matched_review_events", len(text_review)),
        metric("text_no_result_events", len(text_no_result)),
        metric("text_pending_events", len(text_pending)),
        metric("new_events_from_text_matched", len(text_matched - base_events)),
        metric("new_events_from_text_matched_or_review", len(text_any_usable - base_events)),
        metric("geolocated_with_text_matched", len(covered_auto)),
        metric("geolocated_with_text_matched_or_review", len(covered_with_review)),
        metric("remaining_after_text_matched", len(all_events - covered_auto)),
        metric("remaining_after_text_matched_or_review", len(all_events - covered_with_review)),
    ]


def require_columns(frame: pd.DataFrame, columns: Set[str], label: str) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise SystemExit(f"{label} missing column(s): " + ", ".join(sorted(missing)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize how many EM-DAT events are recovered by text geocoding "
            "after direct coordinates and ADM2 AOIs."
        )
    )
    parser.add_argument("--candidates-csv", default=RECENT_EMDAT_GEOCODING_CANDIDATES_CSV)
    parser.add_argument("--adm2-aois-csv", default=RECENT_EMDAT_ADM2_AOIS_CSV)
    parser.add_argument(
        "--text-candidates-csv",
        default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_CANDIDATES_CSV,
    )
    parser.add_argument(
        "--text-results-csv",
        default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_RESULTS_CSV,
    )
    parser.add_argument(
        "--event-coverage-csv",
        default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_EVENT_COVERAGE_CSV,
    )
    parser.add_argument(
        "--summary-csv",
        default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_EVENT_SUMMARY_CSV,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = [
        (Path(args.candidates_csv), "Candidates CSV"),
        (Path(args.adm2_aois_csv), "ADM2 AOIs CSV"),
        (Path(args.text_candidates_csv), "Text candidates CSV"),
        (Path(args.text_results_csv), "Text results CSV"),
    ]
    for path, label in paths:
        if not path.exists():
            raise SystemExit(f"{label} not found: {path}")

    candidates = pd.read_csv(args.candidates_csv)
    adm2_aois = pd.read_csv(args.adm2_aois_csv)
    text_candidates = pd.read_csv(args.text_candidates_csv)
    text_results = pd.read_csv(args.text_results_csv)

    require_columns(
        candidates,
        {EVENT_ID_COLUMN, "has_direct_coordinates"},
        "Candidates CSV",
    )
    require_columns(adm2_aois, {EVENT_ID_COLUMN, "bbox_status"}, "ADM2 AOIs CSV")
    require_columns(
        text_candidates,
        {
            EVENT_ID_COLUMN,
            QUERY_ID_COLUMN,
            "geocoding_query",
            "country",
            "iso",
            "disaster_type",
            "start_date",
        },
        "Text candidates CSV",
    )
    require_columns(
        text_results,
        {QUERY_ID_COLUMN, "geocoding_status"},
        "Text results CSV",
    )

    all_events, direct_events, adm2_events, base_events = base_coverage(
        candidates,
        adm2_aois,
    )
    text_events = text_event_rows(text_candidates, text_results)
    rows = summary_rows(
        all_events=all_events,
        direct_events=direct_events,
        adm2_events=adm2_events,
        base_events=base_events,
        text_events=text_events,
    )
    summary = pd.DataFrame(rows)

    event_coverage_path = Path(args.event_coverage_csv)
    summary_path = Path(args.summary_csv)
    event_coverage_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    text_events.to_csv(event_coverage_path, index=False)
    summary.to_csv(summary_path, index=False)

    print("Text geocoding event coverage summary:")
    for row in rows:
        print(
            f"{row['metric']}: {row['count']} "
            f"({row['percent_of_total']}%)"
        )
    print()
    print(f"Event coverage CSV: {event_coverage_path}")
    print(f"Summary CSV: {summary_path}")


if __name__ == "__main__":
    main()
