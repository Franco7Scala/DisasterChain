from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


SUMMARY_PROMPT_VERSION = "event_news_summary_v1"
INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"

SUMMARY_PROMPT_TEMPLATE = """You are analyzing disaster event records and related news articles.

Task:
Write a concise factual summary of the disaster event using ONLY the information provided in the event metadata and news articles.

Rules:
1. Do not invent facts, numbers, dates, locations, causes, or impacts.
2. Prefer information confirmed by multiple news articles or by the event metadata.
3. Mention the disaster type, location, approximate date, main impacts, and affected population/infrastructure if available.
4. If the news are limited or partially informative, still summarize what is supported and avoid overclaiming.
5. If there is not enough information to summarize the event, output exactly: INSUFFICIENT_INFORMATION.
6. Return only the summary text. Do not include bullet points, headings, explanations, or JSON.

Event metadata:
{event_metadata}

News articles:
{news_articles}

Summary:"""

DEFAULT_METADATA_FIELDS = [
    ("disaster_id", "event_id"),
    ("DisNo.", "emdat_disaster_id"),
    ("Country", "country"),
    ("country", "country"),
    ("ISO", "iso"),
    ("Subregion", "subregion"),
    ("Region", "region"),
    ("region", "region"),
    ("Disaster Group", "disaster_group"),
    ("Disaster Subgroup", "disaster_subgroup"),
    ("Disaster Type", "disaster_type"),
    ("disaster_type", "disaster_type"),
    ("Disaster Subtype", "disaster_subtype"),
    ("Event Name", "event_name"),
    ("Location", "emdat_location"),
    ("start_date", "start_date"),
    ("Start Year", "start_year"),
    ("Start Month", "start_month"),
    ("Start Day", "start_day"),
    ("End Year", "end_year"),
    ("End Month", "end_month"),
    ("End Day", "end_day"),
    ("position_source", "position_source"),
    ("latitude", "final_latitude"),
    ("longitude", "final_longitude"),
    ("Latitude", "emdat_latitude"),
    ("Longitude", "emdat_longitude"),
    ("Total Deaths", "total_deaths"),
    ("No. Injured", "injured"),
    ("No. Affected", "affected"),
    ("No. Homeless", "homeless"),
    ("Total Affected", "total_affected"),
    ("Total Damage ('000 US$)", "total_damage_000_usd"),
]

ARTICLE_TEXT_FIELDS = [
    "raw_text",
    "text",
    "body",
    "summary",
    "description",
    "snippet",
    "title",
]

# Cleans text values before they are inserted into prompts or outputs.
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "[]"}:
        return ""
    return re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()

# Shortens long text while keeping the configured character limit.
def truncate_text(text: str, max_chars: int) -> str:
    text = clean_text(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."

# Finds the event id using the field names used across the project.
def event_id_from_record(record: Mapping[str, Any]) -> str:
    for key in ("disaster_id", "DisNo.", "emdat_disaster_id", "id"):
        value = clean_text(record.get(key))
        if value:
            return value
    return ""

# Merges the main JSON record with optional event-level CSV metadata.
def merge_event_metadata(
    record: Mapping[str, Any],
    event_row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for source in (record, event_row or {}):
        for key, value in source.items():
            if key not in merged or clean_text(merged.get(key)) == "":
                merged[key] = value
    return merged

# Compacts weather summaries when they are available in the original record.
def compact_weather_summary(record: Mapping[str, Any]) -> Dict[str, Any]:
    weather = record.get("weather_data") or {}
    if not isinstance(weather, Mapping):
        return {}

    output: Dict[str, Any] = {}
    for key in ("pre_event_summary", "post_event_summary"):
        value = weather.get(key)
        if isinstance(value, Mapping):
            compact = {
                sub_key: sub_value
                for sub_key, sub_value in value.items()
                if clean_text(sub_value) != ""
            }
            if compact:
                output[key] = compact
    return output

# Converts event metadata into readable lines for the LLM prompt.
def metadata_lines(
    record: Mapping[str, Any],
    event_row: Optional[Mapping[str, Any]] = None,
) -> str:
    metadata = merge_event_metadata(record, event_row)
    lines: List[str] = []
    seen_labels = set()

    for source_key, label in DEFAULT_METADATA_FIELDS:
        if label in seen_labels:
            continue
        value = clean_text(metadata.get(source_key))
        if value:
            lines.append(f"- {label}: {value}")
            seen_labels.add(label)

    weather = compact_weather_summary(record)
    if weather:
        lines.append("- weather_summary: " + json.dumps(weather, ensure_ascii=False))

    if not lines:
        event_id = event_id_from_record(record)
        if event_id:
            lines.append(f"- event_id: {event_id}")

    return "\n".join(lines) if lines else "- metadata: unavailable"

# Chooses the best available text field for a news article.
def article_text(article: Mapping[str, Any], max_article_chars: int) -> str:
    for field in ARTICLE_TEXT_FIELDS:
        value = clean_text(article.get(field))
        if value:
            return truncate_text(value, max_article_chars)
    return ""

# Converts article confidence into a numeric score for sorting.
def confidence_rank(article: Mapping[str, Any]) -> int:
    ranks = {"high": 3, "medium": 2, "low": 1}
    return ranks.get(clean_text(article.get("confidence")).lower(), 0)

# Reads the relevance score already produced by the news pipeline.
def relevance_score(article: Mapping[str, Any]) -> float:
    try:
        return float(article.get("relevance_score") or 0)
    except (TypeError, ValueError):
        return 0.0

# Sorts articles by confidence, relevance, source, and title.
def sorted_articles(articles: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return sorted(
        [article for article in articles if isinstance(article, Mapping)],
        key=lambda article: (
            confidence_rank(article),
            relevance_score(article),
            clean_text(article.get("source")),
            clean_text(article.get("title")),
        ),
        reverse=True,
    )

# Classifies whether the available news text is enough for the LLM.
def news_input_quality(usable_articles: int, total_chars: int) -> str:
    if usable_articles == 0 or total_chars < 200:
        return "insufficient"
    if usable_articles < 2 or total_chars < 1000:
        return "limited"
    return "sufficient"

# Prepares the news block while respecting article and context limits.
def format_articles(
    articles: Iterable[Mapping[str, Any]],
    *,
    max_articles: int = 8,
    max_article_chars: int = 1800,
    max_total_chars: int = 14000,
) -> Tuple[str, Dict[str, Any]]:
    article_list = [article for article in articles if isinstance(article, Mapping)]
    selected = sorted_articles(article_list)[:max_articles]
    formatted: List[str] = []
    total_chars = 0
    sources = set()
    usable_articles = 0

    for index, article in enumerate(selected, start=1):
        source = clean_text(article.get("source")) or "unknown"
        title = clean_text(article.get("title")) or "untitled"
        url = clean_text(article.get("url"))
        published = clean_text(article.get("published_date") or article.get("date"))
        text = article_text(article, max_article_chars)
        if not text:
            continue

        sources.add(source)
        usable_articles += 1
        block_lines = [
            f"Article {index}:",
            f"Source: {source}",
            f"Title: {title}",
        ]
        if published:
            block_lines.append(f"Date: {published}")
        if url:
            block_lines.append(f"URL: {url}")
        block_lines.append(f"Text: {text}")
        block = "\n".join(block_lines)

        if max_total_chars > 0 and total_chars + len(block) > max_total_chars:
            remaining = max_total_chars - total_chars
            if remaining < 500:
                break
            block = truncate_text(block, remaining)

        formatted.append(block)
        total_chars += len(block)

    stats = {
        "news_count": len(article_list),
        "selected_news_count": len(selected),
        "usable_news_count": usable_articles,
        "news_sources_count": len(sources),
        "news_total_chars": total_chars,
        "news_input_quality": news_input_quality(usable_articles, total_chars),
    }
    if not formatted:
        return "No usable news articles available.", stats
    return "\n\n".join(formatted), stats

# Builds the complete event context used by the summary prompt.
def build_event_news_context(
    record: Mapping[str, Any],
    *,
    event_row: Optional[Mapping[str, Any]] = None,
    max_articles: int = 8,
    max_article_chars: int = 1800,
    max_total_chars: int = 14000,
) -> Dict[str, Any]:
    news_data = record.get("news_data") or {}
    articles = []
    if isinstance(news_data, Mapping):
        articles = news_data.get("articles") or []

    news_articles, stats = format_articles(
        articles,
        max_articles=max_articles,
        max_article_chars=max_article_chars,
        max_total_chars=max_total_chars,
    )
    context = {
        "event_id": event_id_from_record(record),
        "event_metadata": metadata_lines(record, event_row),
        "news_articles": news_articles,
    }
    context.update(stats)
    return context

# Inserts event metadata and news articles into the final prompt.
def build_summary_prompt(context: Mapping[str, Any]) -> str:
    return SUMMARY_PROMPT_TEMPLATE.format(
        event_metadata=context.get("event_metadata", "- metadata: unavailable"),
        news_articles=context.get("news_articles", "No usable news articles available."),
    )

# Cleans the LLM response and applies the insufficient-information fallback.
def normalize_summary_response(response: Any) -> str:
    text = clean_text(response)
    if not text:
        return INSUFFICIENT_INFORMATION
    if text.startswith("```"):
        text = re.sub(r"^```(?:text)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^(summary\s*:)\s*", "", text, flags=re.IGNORECASE).strip()
    return text or INSUFFICIENT_INFORMATION

# Builds the prompt, calls the Reasoner, and returns the summary fields.
def summarize_event_from_news(
    reasoner: Any,
    context: Mapping[str, Any],
    *,
    max_new_tokens: int = 256,
) -> Dict[str, Any]:
    prompt = build_summary_prompt(context)
    raw_response = reasoner.ask(prompt, max_new_tokens=max_new_tokens)
    return {
        "event_summary": normalize_summary_response(raw_response),
        "summary_raw_response": raw_response,
        "summary_prompt_version": SUMMARY_PROMPT_VERSION,
    }
