import argparse
import csv
import json
import os

from support.constants import FINAL_DATASET_OUTPUT_PATH, RESULTS_DIR


DEFAULT_OUTPUT_PATH = os.path.join(RESULTS_DIR, "news_benchmark_review.csv")


CSV_COLUMNS = [
    "benchmark_event_number",
    "disaster_id",
    "country",
    "region",
    "disaster_type",
    "start_date",
    "latitude",
    "longitude",
    "news_engine_version",
    "total_articles_for_event",
    "sources_successfully_resolved",
    "article_index",
    "source",
    "title",
    "url",
    "raw_text",
    "relevance_score",
    "relevance_threshold",
    "relevance_reasons",
    "relevance_penalties",
    "search_query",
    "query_precision",
    "published_at",
    "manual_label",
    "manual_notes",
]


def _stringify(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " | ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _load_dataset(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("The final dataset JSON must contain an object keyed by disaster id.")
    return data


def _event_metadata(record, event_number):
    news_data = record.get("news_data") or {}
    search_metadata = news_data.get("search_metadata") or {}
    articles = news_data.get("articles") or []

    return {
        "benchmark_event_number": event_number,
        "disaster_id": record.get("disaster_id", ""),
        "country": record.get("country", ""),
        "region": record.get("region", ""),
        "disaster_type": record.get("disaster_type", ""),
        "start_date": record.get("start_date", ""),
        "latitude": record.get("latitude", ""),
        "longitude": record.get("longitude", ""),
        "news_engine_version": search_metadata.get("news_engine_version", ""),
        "total_articles_for_event": len(articles),
        "sources_successfully_resolved": _stringify(
            search_metadata.get("sources_successfully_resolved", [])
        ),
    }


def _article_metadata(article, article_index):
    return {
        "article_index": article_index,
        "source": article.get("source", ""),
        "title": article.get("title", ""),
        "url": article.get("url", ""),
        "raw_text": article.get("raw_text", ""),
        "relevance_score": article.get("relevance_score", ""),
        "relevance_threshold": article.get("relevance_threshold", ""),
        "relevance_reasons": _stringify(article.get("relevance_reasons", [])),
        "relevance_penalties": _stringify(article.get("relevance_penalties", [])),
        "search_query": article.get("search_query", ""),
        "query_precision": article.get("query_precision", ""),
        "published_at": article.get("published_at", ""),
    }


def build_benchmark_rows(dataset, limit):
    rows = []
    for event_number, (_, record) in enumerate(dataset.items(), start=1):
        if event_number > limit:
            break

        base_row = _event_metadata(record, event_number)
        articles = (record.get("news_data") or {}).get("articles") or []

        if not articles:
            row = dict(base_row)
            row.update(_article_metadata({}, ""))
            row["manual_label"] = ""
            row["manual_notes"] = ""
            rows.append(row)
            continue

        for article_index, article in enumerate(articles, start=1):
            row = dict(base_row)
            row.update(_article_metadata(article, article_index))
            row["manual_label"] = ""
            row["manual_notes"] = ""
            rows.append(row)

    return rows


def write_csv(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create a manual review CSV for disaster news retrieval."
    )
    parser.add_argument(
        "--input",
        default=FINAL_DATASET_OUTPUT_PATH,
        help="Path to final_environmental_causal_dataset.json.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_PATH,
        help="Output CSV path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Number of disaster events to include.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    dataset = _load_dataset(args.input)
    rows = build_benchmark_rows(dataset, args.limit)
    write_csv(rows, args.output)
    print(
        f"Benchmark CSV created: {args.output} "
        f"({args.limit} events, {len(rows)} review rows)."
    )


if __name__ == "__main__":
    main()
