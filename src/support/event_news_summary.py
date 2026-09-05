from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


SUMMARY_PROMPT_VERSION = "event_news_summary_v6"
CAUSAL_CHAIN_PROMPT_VERSION = "event_causal_chain_v8"
INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
FUZZY_QUOTE_MATCH_THRESHOLD = 0.80

SUMMARY_PROMPT_TEMPLATE = """You are an expert journalistic summarizer analyzing disaster event records and related news articles.

Task:
Write a comprehensive, narrative summary of the disaster event. You must weave the structured event metadata and the unstructured news articles together into a cohesive, flowing paragraph.

Rules:
1. Grounding: Do not invent facts, numbers, dates, locations, causes, or impacts. Base your text ONLY on the provided inputs.
2. Event Matching (CRITICAL): Before writing, check whether the news articles directly describe the same event as the metadata. The article must match the event by country/location and disaster type, and it must be temporally compatible with the event date.
3. Ignore unrelated articles: Do not use articles about a different year, a different location, a different disaster type, a generic regional overview, preparedness, forecasts, political response, or a similar but separate event.
4. Metadata is not enough: Use Event Metadata only to identify and anchor the target event. If the News Articles do not directly support the event narrative, output exactly: INSUFFICIENT_INFORMATION.
5. Source Integration: When at least one article is directly relevant, use the Event Metadata for the basic facts and use the News Articles to flesh out the narrative, including physical evolution, human impact, infrastructure damage, rescue efforts, and reported measurements when available.
6. Narrative Style: Write a discursive, encyclopedic paragraph (around 100-150 words). Do not just list metadata facts mechanically. Tell the story of what happened on the ground as reported by the news.
7. Specificity: Include specific details mentioned in the news, such as weather measurements (e.g., "120mm of rain"), exact areas affected, or casualty estimates, if available.
8. Incomplete Data: If the relevant news articles are limited, summarize only what is directly supported and avoid overclaiming. If no directly relevant article remains after the matching check, output exactly: INSUFFICIENT_INFORMATION.
9. Output Format: Return ONLY the summary text. Do not include bullet points, headings, introductory phrases, or JSON.

Event metadata:
{event_metadata}

News articles:
{news_articles}

Summary:"""

CAUSAL_CHAIN_PROMPT_TEMPLATE = """You are analyzing disaster event records and related news articles.

Task:
Extract the causal chain of the disaster event using ONLY the information provided in the event metadata and news articles.

Definition of Causal Chain:
For this task, a "causal chain" is a direct sequence of interconnected physical and socio-economic events where each step explicitly triggers or leads to the next. It typically originates from a meteorological, geological, technological, or human trigger (e.g., Heavy Rain, Earthquake, Mechanical Failure), leads to an intermediate process when reported (e.g., River Overflow, Soil Saturation, Container Rupture), and results in a final physical or social impact (e.g., Bridge Collapse, Flooded Homes, Casualties). A chain can be partial: if the initial trigger is unknown but the article explicitly reports the disaster process and impact, extract the supported steps instead of returning an empty list. Exclude purely political or administrative responses (e.g., declaring a state of emergency) unless they are direct causes of further physical impacts.

Rules:
1. Grounding: Do not invent causal links. Every extracted event must be explicitly supported by the news text.
2. Order: Extract the sequence of relevant causal events in chronological order. If chronology is ambiguous, use a logical cause-to-impact order.
3. Granularity: Break down the event into micro-steps. Do not group multiple consequences into one step. Look specifically for intermediate environmental triggers (e.g., Rainfall -> River overflow -> Bank breach) and cascading socio-economic impacts (e.g., Crop destruction -> Food shortage).
4. Single step: Each item must describe ONE causal step (e.g., trigger, intermediate process, or final consequence).
5. Partial chains: If the initial trigger is not reported, start with the first reported disaster process or accident (e.g., "Storm", "Fire", "Boat Capsizing", "Earthquake") and then add its explicitly reported impacts.
6. Direct impacts: If the news states that the event caused deaths, injuries, displacement, affected people, damage, evacuations, outages, flooding, or other consequences, include those impacts as causal steps.
7. Labeling: Keep "type_event" standardized, short, and reusable as a class label (e.g., "Extreme Precipitation", "Soil Saturation", "Landslide", "Infrastructure Damage", "Displacement").
8. Description: Keep "description" concise (one short sentence).
9. Evidence: You MUST provide a short, exact quote copied from the news articles in the "supporting_quote" field. Prefer 5-25 words from one sentence or title. Do not rewrite, paraphrase, merge separate fragments, or add ellipses unless they appear in the source text.
10. Quote discipline: Use the shortest quote that directly supports the step and avoid metadata-only fields unless the same detail is also present in the news text.
11. Metadata use: Use event metadata only to identify the target event and basic context. Do not create causal steps from metadata alone unless they are also supported by a news quote.
12. Fallback: Return an empty causal_chain list [] only when the news articles do not support any causal disaster step or impact for the target event.
13. Output Format: Return ONLY raw, valid JSON. Do not include explanations, greetings, or markdown formatting like ```json. Start directly with {{ and end with }}.

Required JSON format:
{{
  "causal_chain": [
    {{
      "n_event": 1,
      "type_event": "Extreme Precipitation",
      "description": "Heavy rainfall of 150mm occurred over 24 hours.",
      "supporting_quote": "torrential downpours hit the region on Tuesday"
    }},
    {{
      "n_event": 2,
      "type_event": "Landslide",
      "description": "The saturated soil caused a slope to collapse.",
      "supporting_quote": "the weakened hillside gave way, burying homes"
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

SUMMARY_DIRECT_MATCH_REASONS = {
    "local_place_in_title",
    "local_place_in_text",
    "event_name_in_title",
    "event_name_in_text",
}
SUMMARY_HAZARD_REASONS = {
    "hazard_in_title",
    "hazard_in_text",
    "local_hazard_in_title",
    "local_hazard_in_text",
}
SUMMARY_DATE_REASONS = {
    "event_year_present",
    "event_month_present",
    "publication_date_near_event",
    "publication_date_same_year",
}
SUMMARY_RISKY_PENALTIES = {
    "different_year_in_title",
    "publication_date_far_from_event",
    "missing_local_or_event_context",
    "generic_title",
    "unrelated_multi_year_range",
}
SUMMARY_LOCATION_FIELDS = [
    "event_name",
    "Event Name",
    "emdat_location",
    "Location",
    "location",
    "llm_canonical_location",
    "llm_geocoding_string",
    "geolocation",
    "adm1",
    "ADM1",
    "adm2",
    "ADM2",
    "adm3",
    "ADM3",
]
SUMMARY_DISASTER_TERMS = {
    "air": ["air", "plane", "aircraft", "aviation", "crash"],
    "collapse": ["collapse", "collapsed", "building", "structure"],
    "drought": ["drought", "dry spell", "water shortage"],
    "earthquake": ["earthquake", "quake", "seismic", "tremor"],
    "epidemic": ["epidemic", "outbreak", "disease"],
    "explosion": ["explosion", "blast", "gas leak", "chemical leak"],
    "extreme temperature": ["heatwave", "heat wave", "cold wave", "extreme heat"],
    "fire": ["fire", "blaze", "burned"],
    "flood": ["flood", "flooding", "inundation", "overflow", "heavy rain"],
    "gas leak": ["gas leak", "chlorine", "toxic gas", "chemical leak"],
    "mass movement": ["landslide", "mudslide", "rockslide", "debris flow"],
    "miscellaneous accident": ["accident", "stampede", "incident"],
    "rail": ["rail", "train", "derailment"],
    "road": ["road", "crash", "collision", "traffic accident", "bus"],
    "storm": ["storm", "cyclone", "hurricane", "typhoon", "tropical storm"],
    "water": ["boat", "ship", "ferry", "capsized", "shipwreck", "maritime"],
    "wildfire": ["wildfire", "bushfire", "forest fire", "blaze"],
}
SUMMARY_COUNTRY_ALIASES = {
    "bolivia (plurinational state of)": ["Bolivia"],
    "democratic people's republic of korea": ["North Korea", "DPRK"],
    "democratic republic of the congo": ["DR Congo", "DRC", "Congo-Kinshasa"],
    "iran (islamic republic of)": ["Iran"],
    "lao people's democratic republic": ["Laos"],
    "republic of korea": ["South Korea", "Korea"],
    "russian federation": ["Russia"],
    "syrian arab republic": ["Syria"],
    "taiwan (province of china)": ["Taiwan"],
    "türkiye": ["Turkey"],
    "united republic of tanzania": ["Tanzania"],
    "united states of america": ["United States", "USA", "U.S."],
    "venezuela (bolivarian republic of)": ["Venezuela"],
    "viet nam": ["Vietnam"],
}
SUMMARY_GENERIC_LOCATION_KEYS = {
    "central",
    "eastern",
    "island",
    "islands",
    "national",
    "nationwide",
    "northern",
    "province",
    "provinces",
    "region",
    "regions",
    "several",
    "southern",
    "state",
    "states",
    "western",
}
SUMMARY_UNSUPPORTED_RESPONSE_PATTERNS = [
    r"\balthough\b.*\bnews articles?\b.*\bprimarily focus\b",
    r"\bavailable news articles?\b.*\bprimarily focus\b",
    r"\bprovided news articles?\b.*\bprimarily focus\b",
    r"\bnews articles?\b.*\bdo not directly (report|describe|support|provide)\b",
    r"\bprovided news articles?\b.*\bnot directly (report|describe|support|provide)\b",
    r"\bdoes not provide direct information\b",
    r"\bdo not provide specific details\b",
    r"\bnot directly supported by the provided news articles?\b",
    r"\bbased on the available information, it can be inferred\b",
    r"\bmetadata (confirms|indicates)\b.*\bnews articles?\b.*\bprimarily focus\b",
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


# Converts text to a simple lowercase key for loose matching.
def lookup_key(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Computes a partial fuzzy ratio between a quote and a longer news string.
def partial_sequence_match_ratio(needle: str, haystack: str) -> float:
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    if len(haystack) <= len(needle):
        return SequenceMatcher(None, needle, haystack, autojunk=False).ratio()

    matcher = SequenceMatcher(None, needle, haystack, autojunk=False)
    best_ratio = 0.0
    for match in matcher.get_matching_blocks():
        start = max(match.b - match.a, 0)
        window = haystack[start : start + len(needle)]
        if not window:
            continue
        ratio = SequenceMatcher(None, needle, window, autojunk=False).ratio()
        best_ratio = max(best_ratio, ratio)
        if best_ratio >= FUZZY_QUOTE_MATCH_THRESHOLD:
            break
    return best_ratio


# Computes how many quote tokens can be found in order inside the news text.
def token_sequence_match_ratio(quote_key: str, news_key: str) -> float:
    quote_tokens = quote_key.split()
    news_tokens = news_key.split()
    if len(quote_tokens) < 6 or not news_tokens:
        return 0.0

    matcher = SequenceMatcher(None, quote_tokens, news_tokens, autojunk=False)
    matched_tokens = sum(match.size for match in matcher.get_matching_blocks())
    return matched_tokens / len(quote_tokens)


# Removes descriptive prefixes from EM-DAT location fragments.
def clean_location_piece(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(
        r"^(near|around|between|in|at|outskirts of|villages near|slums of)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(city|cities|districts?|provinces?|regions?|states?|municipalities|villages?)$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return clean_text(text)


# Reads list-like audit fields saved by the news pipeline.
def list_field(value: Any) -> List[str]:
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    text = clean_text(value)
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text.replace("'", '"'))
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            return [clean_text(item) for item in parsed if clean_text(item)]
    return [clean_text(item) for item in re.split(r"[|,;]+", text) if clean_text(item)]


# Extracts a numeric field saved by the news pipeline.
def numeric_field(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# Returns the year in the event metadata when available.
def event_year_from_metadata(metadata: Mapping[str, Any]) -> str:
    direct = clean_text(
        metadata.get("start_date")
        or metadata.get("Start Date")
        or metadata.get("_llm_start_date")
    )
    match = re.search(r"\b(19\d{2}|20\d{2})\b", direct)
    if match:
        return match.group(1)
    return clean_text(metadata.get("Start Year") or metadata.get("start_year"))


# Reads the publication year from common article date fields.
def article_publication_year(article: Mapping[str, Any]) -> str:
    for key in ("published_date", "date", "seendate", "published_at"):
        value = clean_text(article.get(key))
        if not value:
            continue
        try:
            return str(parsedate_to_datetime(value).year)
        except (TypeError, ValueError, IndexError, AttributeError):
            match = re.search(r"\b(19\d{2}|20\d{2})\b", value)
            if match:
                return match.group(1)
    return ""


# Detects title or URL years that clearly point to another event.
def has_wrong_title_or_url_year(article: Mapping[str, Any], event_year: str) -> bool:
    if not event_year:
        return False
    title = clean_text(article.get("title"))
    url = clean_text(article.get("url"))
    years = set(re.findall(r"\b(19\d{2}|20\d{2})\b", title))
    years.update(
        re.findall(
            r"(?:^|[/_-])((?:19|20)\d{2})(?:[/_-]|$)",
            url,
            flags=re.IGNORECASE,
        )
    )
    years.update(
        re.findall(
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_]\d{1,2}[-_]((?:19|20)\d{2})",
            url,
            flags=re.IGNORECASE,
        )
    )
    return bool(years and event_year not in years)


# Extracts coarse location terms from event metadata.
def metadata_location_terms(metadata: Mapping[str, Any]) -> List[str]:
    terms: List[str] = []
    for key in SUMMARY_LOCATION_FIELDS:
        value = clean_text(metadata.get(key))
        if not value:
            continue
        pieces = re.split(r"[,;/()]+|\band\b|\bet\b|\be\b", value, flags=re.IGNORECASE)
        for piece in pieces:
            cleaned = clean_location_piece(piece)
            if len(cleaned) >= 3:
                terms.append(cleaned)
    return list(dict.fromkeys(terms))


# Returns country names and aliases used for country-level matching.
def metadata_country_terms(metadata: Mapping[str, Any]) -> List[str]:
    country = clean_text(metadata.get("country") or metadata.get("Country"))
    if not country:
        return []
    terms = [country]
    terms.extend(SUMMARY_COUNTRY_ALIASES.get(country.lower(), []))
    if "(" in country:
        terms.append(country.split("(")[0].strip())
    return list(dict.fromkeys(term for term in terms if clean_text(term)))


# Keeps only location terms that are more specific than the country.
def specific_metadata_location_terms(metadata: Mapping[str, Any]) -> List[str]:
    country_keys = {lookup_key(term) for term in metadata_country_terms(metadata)}
    specific_terms: List[str] = []
    for term in metadata_location_terms(metadata):
        term_key = lookup_key(term)
        if not term_key or term_key in country_keys:
            continue
        if term_key in SUMMARY_GENERIC_LOCATION_KEYS:
            continue
        specific_terms.append(term)
    return list(dict.fromkeys(specific_terms))


# Checks a plain-text match against normalized whole words.
def text_mentions_term(text: str, term: str) -> bool:
    text_key = lookup_key(text)
    term_key = lookup_key(term)
    if len(term_key) < 3:
        return False
    pattern = rf"(^|\s){re.escape(term_key)}(\s|$)"
    return re.search(pattern, text_key) is not None


# Checks a plain-text fallback match when article audit fields are missing.
def text_mentions_any(text: str, terms: Iterable[str]) -> bool:
    text_key = lookup_key(text)
    if not text_key:
        return False
    for term in terms:
        if text_mentions_term(text_key, term):
            return True
    return False


# Returns disaster keywords for a coarse fallback relevance check.
def disaster_terms(disaster_type: Any) -> List[str]:
    key = lookup_key(str(disaster_type or "").split("(")[0])
    for disaster_key, terms in SUMMARY_DISASTER_TERMS.items():
        if disaster_key in key:
            return terms
    return [key] if key else []


# Checks whether one article is direct enough to be sent to the summary LLM.
def article_is_directly_relevant_for_summary(
    article: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> bool:
    reasons = set(list_field(article.get("relevance_reasons")))
    penalties = set(list_field(article.get("relevance_penalties")))
    confidence = clean_text(article.get("confidence")).lower()
    source = clean_text(article.get("source"))
    event_year = event_year_from_metadata(metadata)

    if penalties & SUMMARY_RISKY_PENALTIES:
        return False
    if has_wrong_title_or_url_year(article, event_year):
        return False

    published_year = article_publication_year(article)
    if event_year and published_year and abs(int(published_year) - int(event_year)) > 1:
        return False

    has_hazard = bool(reasons & SUMMARY_HAZARD_REASONS)
    has_date = bool(reasons & SUMMARY_DATE_REASONS)
    title_and_text = " ".join(
        clean_text(article.get(key))
        for key in ("title", "raw_text", "text", "description", "snippet")
        if clean_text(article.get(key))
    )
    fallback_has_hazard = text_mentions_any(title_and_text, disaster_terms(metadata.get("disaster_type") or metadata.get("Disaster Type")))
    hazard_matches = has_hazard or fallback_has_hazard
    fallback_has_year = bool(event_year and event_year in title_and_text)
    date_matches = has_date or fallback_has_year or bool(published_year and published_year == event_year)

    if not hazard_matches or not date_matches or confidence == "low":
        return False

    location_terms = specific_metadata_location_terms(metadata)
    country_terms = metadata_country_terms(metadata)
    has_specific_place = text_mentions_any(title_and_text, location_terms)
    has_country = text_mentions_any(title_and_text, country_terms)

    if location_terms:
        return has_specific_place

    trusted_source = source.startswith(("ReliefWeb", "GDACS", "IFRC GO", "FloodList"))
    if trusted_source and confidence in {"high", "medium"}:
        return has_country
    return has_country and confidence == "high"


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
    metadata: Optional[Mapping[str, Any]] = None,
    max_articles: int = 8,
    max_article_chars: int = 1800,
    max_total_chars: int = 14000,
) -> Tuple[str, Dict[str, Any]]:
    article_list = [article for article in articles if isinstance(article, Mapping)]
    if metadata is not None:
        relevant_articles = [
            article
            for article in article_list
            if article_is_directly_relevant_for_summary(article, metadata)
        ]
    else:
        relevant_articles = article_list
    selected = sorted_articles(relevant_articles)[:max_articles]
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
        "relevant_news_count": len(relevant_articles),
        "news_rejected_by_relevance_filter": len(article_list) - len(relevant_articles),
        "summary_relevant_news_count": len(relevant_articles),
        "news_rejected_for_summary": len(article_list) - len(relevant_articles),
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

    metadata = merge_event_metadata(record, event_row)
    news_articles, stats = format_articles(
        articles,
        metadata=metadata,
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


# Detects summaries where the model admits the evidence is not event-specific.
def summary_looks_unsupported(summary: Any) -> bool:
    text = clean_text(summary)
    if not text or text == INSUFFICIENT_INFORMATION:
        return False
    return any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in SUMMARY_UNSUPPORTED_RESPONSE_PATTERNS
    )

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


# Returns how much of the quote can be matched inside the news block.
def quote_news_match_ratio(quote: Any, news_articles: Any) -> float:
    quote_text = clean_text(quote)
    news_text = clean_text(news_articles)
    if quote_text and quote_text in news_text:
        return 1.0

    quote_key = lookup_key(quote)
    news_key = lookup_key(news_articles)
    if len(quote_key) < 10 or not news_key:
        return 0.0
    if quote_key in news_key:
        return 1.0

    return max(
        partial_sequence_match_ratio(quote_key, news_key),
        token_sequence_match_ratio(quote_key, news_key),
    )


# Checks that the supporting quote is close enough to text in the news block.
def quote_is_supported_by_news(quote: Any, news_articles: Any) -> Tuple[bool, bool]:
    match_ratio = quote_news_match_ratio(quote, news_articles)
    if match_ratio >= 1.0:
        return True, False
    if match_ratio >= FUZZY_QUOTE_MATCH_THRESHOLD:
        return True, True
    return False, False


# Drops causal-chain steps whose evidence quote is not found in the news text.
def validate_causal_chain_quotes(
    causal_chain: List[Dict[str, Any]],
    news_articles: Any,
) -> Tuple[List[Dict[str, Any]], int, int]:
    validated: List[Dict[str, Any]] = []
    dropped = 0
    fuzzy_matched = 0
    for item in causal_chain:
        is_supported, used_fuzzy = quote_is_supported_by_news(
            item.get("supporting_quote"),
            news_articles,
        )
        if is_supported:
            validated_item = dict(item)
            validated_item["n_event"] = len(validated) + 1
            validated.append(validated_item)
            if used_fuzzy:
                fuzzy_matched += 1
        else:
            dropped += 1
    return validated, dropped, fuzzy_matched


# Builds the prompt, calls the Reasoner, and returns the summary fields.
def summarize_event_from_news(
    reasoner: Any,
    context: Mapping[str, Any],
    *,
    max_new_tokens: int = 256,
) -> Dict[str, Any]:
    prompt = build_summary_prompt(context)
    raw_response = reasoner.ask(prompt, max_new_tokens=max_new_tokens)
    event_summary = normalize_summary_response(raw_response)
    validation_status = "accepted"
    if summary_looks_unsupported(event_summary):
        event_summary = INSUFFICIENT_INFORMATION
        validation_status = "rejected_unrelated_news_admission"
    return {
        "event_summary": event_summary,
        "summary_raw_response": raw_response,
        "summary_prompt_version": SUMMARY_PROMPT_VERSION,
        "summary_validation_status": validation_status,
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
    causal_chain, dropped_quotes, fuzzy_quote_steps = validate_causal_chain_quotes(
        causal_chain,
        context.get("news_articles", ""),
    )
    if dropped_quotes:
        parse_status = (
            "empty_chain_after_quote_validation"
            if not causal_chain
            else "parsed_with_dropped_unsupported_quotes"
        )
    return {
        "causal_chain": causal_chain,
        "causal_chain_json": json.dumps({"causal_chain": causal_chain}, ensure_ascii=False),
        "causal_chain_length": len(causal_chain),
        "causal_chain_parse_status": parse_status,
        "causal_chain_raw_response": raw_response,
        "causal_chain_prompt_version": CAUSAL_CHAIN_PROMPT_VERSION,
        "causal_chain_dropped_quote_steps": dropped_quotes,
        "causal_chain_fuzzy_quote_steps": fuzzy_quote_steps,
    }
