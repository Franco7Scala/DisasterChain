from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


SUMMARY_PROMPT_VERSION = "event_news_summary_v2"
CAUSAL_CHAIN_PROMPT_VERSION = "event_causal_chain_v1"
INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"

SUMMARY_PROMPT_TEMPLATE = """You are an expert journalistic summarizer analyzing disaster event records and related news articles.

Task:
Write a comprehensive, narrative summary of the disaster event. You must weave the structured event metadata and the unstructured news articles together into a cohesive, flowing paragraph.

Rules:
1. Grounding: Do not invent facts, numbers, dates, locations, causes, or impacts. Base your text ONLY on the provided inputs.
2. Source Integration (CRITICAL): Use the Event Metadata to anchor the basic facts (date, location, disaster type). You MUST use the News Articles to flesh out the narrative (e.g., the physical evolution of the event, human impact, infrastructure damage, and rescue efforts).
3. Narrative Style: Write a discursive, encyclopedic paragraph (around 100-150 words). Do not just list metadata facts mechanically. Tell the story of what happened on the ground as reported by the news.
4. Specificity: Include specific details mentioned in the news, such as weather measurements (e.g., "120mm of rain"), exact areas affected, or casualty estimates, if available.
5. Incomplete Data: If the news articles are limited, summarize what is supported and avoid overclaiming. If there is absolutely not enough information in both sources to write a summary, output exactly: INSUFFICIENT_INFORMATION.
6. Output Format: Return ONLY the summary text. Do not include bullet points, headings, introductory phrases, or JSON.

Event metadata:
{event_metadata}

News articles:
{news_articles}

Summary:"""

CAUSAL_CHAIN_PROMPT_TEMPLATE = """You are analyzing disaster event records and related news articles.

Task:
Extract the causal chain of the disaster event using ONLY the information provided in the event metadata and news articles.

Definition of Causal Chain:
For this task, a "causal chain" is a direct sequence of interconnected physical and socio-economic events where each step explicitly triggers the next. It typically originates from a meteorological or geological trigger (e.g., Heavy Rain), leads to an intermediate environmental change (e.g., River Overflow, Soil Saturation), and results in a final physical or social impact (e.g., Bridge Collapse, Flooded Homes, Casualties). Exclude purely political or administrative responses (e.g., declaring a state of emergency) unless they are direct causes of further physical impacts.

Rules:
1. Grounding: Do not invent causal links. Every extracted event must be explicitly supported by the text.
2. Order: Extract the sequence of relevant causal events in chronological order. If chronology is ambiguous, use a logical cause-to-impact order.
3. Granularity: Each item must describe ONE causal step (e.g., trigger, intermediate process, or final consequence).
4. Labeling: Keep "type_event" standardized, short, and reusable as a class label (e.g., "Extreme Precipitation", "Soil Saturation", "Landslide", "Infrastructure Damage", "Displacement").
5. Description: Keep "description" concise (one short sentence).
6. Evidence: You MUST provide a short, exact quote from the news articles in the "supporting_quote" field to prove the event occurred.
7. Fallback: If the causal chain cannot be extracted from the available information, return an empty causal_chain list [].
8. Output Format: Return ONLY raw, valid JSON. Do not include explanations, greetings, or markdown formatting like `json. Start directly with {{ and end with }}.

Required JSON format:
{{
  "causal_chain": [
    {{
      "n_event": 1,
      "type_event": "Extreme Precipitation",
      "description": "Heavy rainfall of 150mm occurred over 24 hours.",
      "supporting_quote": "...torrential downpours hit the region on Tuesday..."
    }},
    {{
      "n_event": 2,
      "type_event": "Landslide",
      "description": "The saturated soil caused a slope to collapse.",
      "supporting_quote": "...the weakened hillside gave way, burying homes..."
    }}
  ]
}}

Event metadata:
{event_metadata}

News articles:
{news_articles}"""

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

# Inserts event metadata and news articles into the causal-chain prompt.
def build_causal_chain_prompt(context: Mapping[str, Any]) -> str:
    return CAUSAL_CHAIN_PROMPT_TEMPLATE.format(
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

# Extracts the first JSON object from an LLM response.
def response_json_text(response: Any) -> str:
    text = clean_text(response)
    if not text:
        return ""
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start : end + 1]

# Normalizes one causal-chain step returned by the LLM.
def normalize_causal_chain_item(item: Mapping[str, Any], sequence_number: int) -> Optional[Dict[str, Any]]:
    type_event = clean_text(item.get("type_event"))
    description = clean_text(item.get("description"))
    supporting_quote = clean_text(item.get("supporting_quote"))
    if not type_event or not description or not supporting_quote:
        return None
    return {
        "n_event": sequence_number,
        "type_event": type_event,
        "description": description,
        "supporting_quote": supporting_quote,
    }

# Parses and validates the causal-chain JSON returned by the LLM.
def parse_causal_chain_response(response: Any) -> Tuple[List[Dict[str, Any]], str]:
    json_text = response_json_text(response)
    if not json_text:
        return [], "invalid_json"

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return [], "invalid_json"

    chain = payload.get("causal_chain")
    if not isinstance(chain, list):
        return [], "missing_causal_chain"

    normalized: List[Dict[str, Any]] = []
    dropped = 0
    for item in chain:
        if not isinstance(item, Mapping):
            dropped += 1
            continue
        normalized_item = normalize_causal_chain_item(item, len(normalized) + 1)
        if normalized_item is None:
            dropped += 1
            continue
        normalized.append(normalized_item)

    if not normalized:
        return [], "empty_chain"
    if dropped:
        return normalized, "parsed_with_dropped_items"
    return normalized, "parsed"

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

# Builds the prompt, calls the Reasoner, and returns causal-chain fields.
def extract_causal_chain_from_news(
    reasoner: Any,
    context: Mapping[str, Any],
    *,
    max_new_tokens: int = 512,
) -> Dict[str, Any]:
    prompt = build_causal_chain_prompt(context)
    raw_response = reasoner.ask(prompt, max_new_tokens=max_new_tokens)
    causal_chain, parse_status = parse_causal_chain_response(raw_response)
    return {
        "causal_chain": causal_chain,
        "causal_chain_json": json.dumps({"causal_chain": causal_chain}, ensure_ascii=False),
        "causal_chain_length": len(causal_chain),
        "causal_chain_parse_status": parse_status,
        "causal_chain_raw_response": raw_response,
        "causal_chain_prompt_version": CAUSAL_CHAIN_PROMPT_VERSION,
    }
