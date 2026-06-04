import argparse
import csv
import json
import os
from collections import Counter, defaultdict

from support.constants import *


def _load_dataset(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("The final dataset JSON must contain an object keyed by disaster id.")
    return data


def _source_family(source):
    source = str(source or "missing")
    if source.startswith("Google News"):
        return "Google News"
    if source.startswith("DuckDuckGo"):
        return "DuckDuckGo"
    return source


def _confidence(article):
    return str(article.get("confidence") or "missing")


def collect_source_stats(dataset):
    stats = defaultdict(lambda: {
        "articles": 0,
        "events": set(),
        "high": 0,
        "medium": 0,
        "low": 0,
        "missing_confidence": 0,
        "non_article_candidates": 0,
        "score_sum": 0.0,
        "score_count": 0,
    })

    total_events = len(dataset)
    events_with_news = 0
    queried_counts = Counter()
    resolved_counts = Counter()

    for disaster_id, record in dataset.items():
        news_data = record.get("news_data") or {}
        search_metadata = news_data.get("search_metadata") or {}
        articles = news_data.get("articles") or []

        if articles:
            events_with_news += 1

        for source in search_metadata.get("global_sources_queried", []):
            queried_counts[source] += 1
        for source in search_metadata.get("regional_sources_queried", []):
            queried_counts[source] += 1
        for source in search_metadata.get("fallback_sources_queried", []):
            queried_counts[source] += 1

        for source in search_metadata.get("sources_successfully_resolved", []):
            resolved_counts[_source_family(source)] += 1

        for article in articles:
            source = _source_family(article.get("source"))
            source_stats = stats[source]
            source_stats["articles"] += 1
            source_stats["events"].add(disaster_id)

            confidence = _confidence(article)
            if confidence == "high":
                source_stats["high"] += 1
            elif confidence == "medium":
                source_stats["medium"] += 1
            elif confidence == "low":
                source_stats["low"] += 1
            else:
                source_stats["missing_confidence"] += 1

            if article.get("is_non_article_candidate"):
                source_stats["non_article_candidates"] += 1

            score = article.get("relevance_score")
            if isinstance(score, (int, float)):
                source_stats["score_sum"] += float(score)
                source_stats["score_count"] += 1

    rows = []
    for source, source_stats in stats.items():
        score_count = source_stats["score_count"]
        avg_score = (
            source_stats["score_sum"] / score_count
            if score_count else 0.0
        )
        rows.append({
            "source": source,
            "articles": source_stats["articles"],
            "events_with_articles": len(source_stats["events"]),
            "high_confidence": source_stats["high"],
            "medium_confidence": source_stats["medium"],
            "low_confidence": source_stats["low"],
            "missing_confidence": source_stats["missing_confidence"],
            "non_article_candidates": source_stats["non_article_candidates"],
            "avg_relevance_score": round(avg_score, 2),
            "metadata_resolved_count": resolved_counts[source],
        })

    rows.sort(key=lambda row: row["articles"], reverse=True)

    summary = {
        "total_events": total_events,
        "events_with_news": events_with_news,
        "success_rate": (events_with_news / total_events) if total_events else 0.0,
        "total_articles": sum(row["articles"] for row in rows),
        "queried_counts": queried_counts,
        "sources_with_articles": len(rows),
    }
    return summary, rows


def build_report(summary, rows):
    lines = []
    lines.append("NEWS SOURCE STATISTICS")
    lines.append("=" * 70)
    lines.append(f"Total events: {summary['total_events']}")
    lines.append(f"Events with news: {summary['events_with_news']}")
    lines.append(f"Success rate: {summary['success_rate']:.2%}")
    lines.append(f"Total articles: {summary['total_articles']}")
    lines.append(f"Sources with at least one article: {summary['sources_with_articles']}")
    lines.append("")

    lines.append("Articles by source")
    lines.append("-" * 70)
    for row in rows:
        lines.append(
            f"{row['source']}: articles={row['articles']}, "
            f"events={row['events_with_articles']}, "
            f"confidence high/medium/low/missing="
            f"{row['high_confidence']}/{row['medium_confidence']}/"
            f"{row['low_confidence']}/{row['missing_confidence']}, "
            f"non_article_candidates={row['non_article_candidates']}, "
            f"avg_score={row['avg_relevance_score']}"
        )
    lines.append("")

    lines.append("Queried source counts")
    lines.append("-" * 70)
    for source, count in sorted(summary["queried_counts"].items()):
        lines.append(f"{source}: queried_for_events={count}")

    return "\n".join(lines)


def write_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    columns = [
        "source",
        "articles",
        "events_with_articles",
        "high_confidence",
        "medium_confidence",
        "low_confidence",
        "missing_confidence",
        "non_article_candidates",
        "avg_relevance_score",
        "metadata_resolved_count",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_report(report, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Count retrieved news articles by source."
    )
    parser.add_argument(
        "--input",
        default=FINAL_DATASET_OUTPUT_PATH,
        help="Path to final_environmental_causal_dataset.json.",
    )
    parser.add_argument(
        "--report-output",
        default=DEFAULT_REPORT_PATH,
        help="Path for the text report.",
    )
    parser.add_argument(
        "--csv-output",
        default=DEFAULT_CSV_PATH,
        help="Path for the CSV report.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    dataset = _load_dataset(args.input)
    summary, rows = collect_source_stats(dataset)
    report = build_report(summary, rows)
    write_report(report, args.report_output)
    write_csv(rows, args.csv_output)

    print(report)
    print("")
    print(f"Report written to: {args.report_output}")
    print(f"CSV written to: {args.csv_output}")


if __name__ == "__main__":
    main()
