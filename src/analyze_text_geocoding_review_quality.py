import argparse
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

from support.constants import (
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_EVENT_COVERAGE_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_REVIEW_QUALITY_CSV,
    RECENT_EMDAT_NATURAL_TEXT_GEOCODING_REVIEW_QUALITY_SUMMARY_CSV,
)


REVIEW_STATUS = "matched_review"
TINY_BBOX_MAX_SIDE_DEG = 0.02
SMALL_BBOX_MAX_SIDE_DEG = 0.1
LARGE_BBOX_MIN_SIDE_DEG = 8.0
VERY_LARGE_BBOX_MIN_SIDE_DEG = 15.0
COMPOSITE_QUERY_RE = re.compile(r"\b(?:and|including)\b", flags=re.IGNORECASE)
POI_KEYWORDS = {
    "aerial service",
    "airport",
    "bank",
    "building",
    "campus",
    "casino",
    "church",
    "college",
    "consulate",
    "hotel",
    "museum",
    "peak",
    "post office",
    "road",
    "school",
    "street",
    "tunnel",
    "university",
}


def numeric(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def has_poi_keyword(display_name: str) -> bool:
    text = str(display_name or "").lower()
    return any(keyword in text for keyword in POI_KEYWORDS)


def has_composite_query(query: str) -> bool:
    text = str(query or "")
    if COMPOSITE_QUERY_RE.search(text):
        return True
    return text.count(",") >= 2


def classify_review(row: pd.Series) -> Tuple[str, str]:
    width = numeric(row.get("bbox_width_deg"))
    height = numeric(row.get("bbox_height_deg"))
    max_side = max(width, height)
    reasons: List[str] = []

    if max_side <= 0:
        reasons.append("missing_bbox")
    elif max_side <= TINY_BBOX_MAX_SIDE_DEG:
        reasons.append("tiny_bbox")
    elif max_side <= SMALL_BBOX_MAX_SIDE_DEG:
        reasons.append("small_bbox")

    if max_side >= VERY_LARGE_BBOX_MIN_SIDE_DEG:
        reasons.append("very_large_bbox")
    elif max_side >= LARGE_BBOX_MIN_SIDE_DEG:
        reasons.append("large_bbox")

    if str(row.get("best_query_variant") or "") == "split_component":
        reasons.append("split_component_query")

    query = str(row.get("best_geocoding_query") or "")
    if has_composite_query(query):
        reasons.append("composite_query")

    if has_poi_keyword(str(row.get("best_display_name") or "")):
        reasons.append("possible_poi_result")

    if any(reason in reasons for reason in ["missing_bbox", "tiny_bbox", "small_bbox", "possible_poi_result"]):
        bucket = "high_risk_review"
    elif any(reason in reasons for reason in ["very_large_bbox", "large_bbox"]):
        bucket = "large_area_review"
    elif any(reason in reasons for reason in ["split_component_query", "composite_query"]):
        bucket = "composite_review"
    else:
        bucket = "review_candidate"

    return bucket, ",".join(reasons)


def summary_rows(review: pd.DataFrame) -> List[Dict]:
    rows: List[Dict] = []
    total = len(review)

    def add(metric: str, count: int) -> None:
        rows.append(
            {
                "metric": metric,
                "count": int(count),
                "percent_of_review": round(count / total * 100, 2) if total else 0.0,
            }
        )

    add("review_events", total)
    for bucket, count in review["review_quality_bucket"].value_counts().items():
        add(f"bucket_{bucket}", count)

    reason_counts: Dict[str, int] = {}
    for value in review["review_quality_reasons"].dropna().astype(str):
        for reason in [item for item in value.split(",") if item]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, count in sorted(reason_counts.items()):
        add(f"reason_{reason}", count)
    return rows


def require_columns(frame: pd.DataFrame, columns: set[str]) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise SystemExit("Event coverage CSV missing column(s): " + ", ".join(sorted(missing)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Classify matched_review text-geocoding events into broad review "
            "quality buckets for manual inspection."
        )
    )
    parser.add_argument(
        "--event-coverage-csv",
        default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_EVENT_COVERAGE_CSV,
    )
    parser.add_argument(
        "--review-quality-csv",
        default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_REVIEW_QUALITY_CSV,
    )
    parser.add_argument(
        "--summary-csv",
        default=RECENT_EMDAT_NATURAL_TEXT_GEOCODING_REVIEW_QUALITY_SUMMARY_CSV,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    event_coverage_path = Path(args.event_coverage_csv)
    if not event_coverage_path.exists():
        raise SystemExit(
            "Text geocoding event coverage CSV not found.\n"
            f"Expected path: {event_coverage_path}\n\n"
            "Run summarize_text_geocoding_events.py first."
        )

    events = pd.read_csv(event_coverage_path)
    require_columns(
        events,
        {
            "text_event_status",
            "bbox_width_deg",
            "bbox_height_deg",
            "best_geocoding_query",
            "best_query_variant",
            "best_display_name",
        },
    )

    review = events[events["text_event_status"].eq(REVIEW_STATUS)].copy()
    if len(review):
        classified = review.apply(classify_review, axis=1, result_type="expand")
        review["review_quality_bucket"] = classified[0]
        review["review_quality_reasons"] = classified[1]
    else:
        review["review_quality_bucket"] = []
        review["review_quality_reasons"] = []

    summary = pd.DataFrame(summary_rows(review))
    review_path = Path(args.review_quality_csv)
    summary_path = Path(args.summary_csv)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    review.to_csv(review_path, index=False)
    summary.to_csv(summary_path, index=False)

    print("Text geocoding review quality summary:")
    for _, row in summary.iterrows():
        print(
            f"{row['metric']}: {row['count']} "
            f"({row['percent_of_review']}%)"
        )
    print()
    print(f"Review quality CSV: {review_path}")
    print(f"Summary CSV: {summary_path}")


if __name__ == "__main__":
    main()
