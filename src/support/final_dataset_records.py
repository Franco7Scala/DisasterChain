"""Build the public event fields without calling models or external services."""

import csv
import json
import math
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit


MISSING = "none"
ID_FIELDS = ("event_id", "DisNo.", "disaster_id", "emdat_disaster_id")
POSITION_SOURCES = {"em-dat", "ADM/GADM", "llm_nominatim", "llm_nominatim_review_accepted"}
WEATHER_FIELDS = ("rain_sum", "snowfall_sum", "temperature_2m_max", "temperature_2m_min")
CAUSAL_NUMBERS = (
    "news_count", "causal_relevant_news_count", "news_rejected_for_causal_chain",
    "selected_news_count", "usable_news_count", "news_sources_count", "news_total_chars",
    "causal_chain_dropped_quote_steps", "causal_chain_fuzzy_quote_steps",
    "causal_chain_type_events_changed",
)
CAUSAL_TEXT = (
    "model_name", "llm_call_status", "summary_validation_status", "news_input_quality",
    "causal_chain_parse_status", "causal_chain_prompt_version", "causal_chain_type_normalizer_version",
)


# Recognizes absent scalars without confusing zero and false with missing data.
def missing(value):
    return (value is None or isinstance(value, float) and not math.isfinite(value)
            or isinstance(value, str) and value.strip().casefold() in {"", "none", "null", "nan", "nat"})


def first(row, *names):
    return next((row[name] for name in names if name in row and not missing(row[name])), None)


# Converts known numeric fields while keeping missing and invalid values separate from zero.
def number(value, *, count=False):
    if missing(value) or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    if count:
        return int(result) if result >= 0 and result.is_integer() else None
    return result


# Applies the agreed missing-value convention only at the serialization boundary.
def public_values(value):
    if isinstance(value, dict):
        return {key: public_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_values(item) for item in value]
    return MISSING if missing(value) else value


# Rejects duplicate keys rather than silently discarding an event or a nested field.
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)


# Loads event tables without pandas type inference or silent duplicate-id selection.
def read_records(path):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            if len(header) != len(set(header)) or not set(header).intersection(ID_FIELDS):
                raise ValueError(f"Missing id column or duplicate columns: {path}")
            pairs = [(None, row) for row in reader]
    elif path.suffix.lower() == ".jsonl":
        pairs = [(None, json.loads(line, object_pairs_hook=unique_object))
                 for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    else:
        data = read_json(path)
        if not isinstance(data, (dict, list)):
            raise ValueError(f"Expected an event dictionary or list: {path}")
        pairs = list(data.items()) if isinstance(data, dict) else [(None, row) for row in data]
    records = {}
    for key, row in pairs:
        if not isinstance(row, dict) or None in row:
            raise ValueError(f"Malformed event record: {path}")
        row_id = first(row, *ID_FIELDS)
        event_id = str(key if key is not None else row_id or "").strip()
        if not event_id or event_id in records or row_id is not None and str(row_id).strip() != event_id:
            raise ValueError(f"Missing, duplicate or inconsistent event id {event_id!r}: {path}")
        records[event_id] = row
    return records


# Uses saved dates or complete EM-DAT date parts without inventing missing months or days.
def event_date(row):
    direct = first(row, "event_date", "start_date", "_llm_start_date", "Start Date")
    try:
        if direct is not None:
            return date.fromisoformat(str(direct).strip()[:10])
        parts = [number(first(row, f"Start {name}", f"start_{name.lower()}"), count=True)
                 for name in ("Year", "Month", "Day")]
        return date(*parts) if all(part is not None for part in parts) else None
    except (ValueError, TypeError):
        return None


def window_dates(start):
    return [(start + timedelta(days=offset)).isoformat() for offset in range(-10, 11)]


# Maps EM-DAT fields and converts the original damage amount from thousands of USD.
def event_fields(event_id, row, start):
    damage = number(row.get("total_damage_usd"))
    if damage is None:
        thousands = number(first(row, "Total Damage ('000 US$)", "Total Damages ('000 US$)", "total_damage_000_usd"))
        damage = thousands * 1000 if thousands is not None and thousands >= 0 else None
    return {
        "disaster_id": event_id,
        "disaster_type": first(row, "Disaster Type", "disaster_type"),
        "country": first(row, "Country", "country"),
        "region": first(row, "Region", "region"),
        "latitude": number(row.get("latitude")),
        "longitude": number(row.get("longitude")),
        "event_date": start.isoformat(),
        "total_deaths": number(first(row, "Total Deaths", "total_deaths"), count=True),
        "total_affected": number(first(row, "Total Affected", "total_affected"), count=True),
        "total_damage_usd": damage if damage is not None and damage >= 0 else None,
        "position_source": row.get("position_source"),
    }


# Aligns weather by date and computes means without counting missing days as zero.
def weather_fields(record, start, warnings):
    weather = (record or {}).get("weather_data")
    if not isinstance(weather, dict) or not isinstance(weather.get("daily_series"), dict):
        warnings.append("weather_unavailable")
        return None
    daily = weather["daily_series"]
    times = daily.get("time", [])
    if not isinstance(times, list) or len(times) != len(set(times)):
        raise ValueError("Duplicate or invalid weather dates")
    for field in WEATHER_FIELDS:
        if field in daily and (not isinstance(daily[field], list) or len(daily[field]) != len(times)):
            raise ValueError(f"Weather array length mismatch: {field}")
    lookup = {str(day): index for index, day in enumerate(times)}
    provider = first(weather, "provider") or first(record, "weather_provider") or daily.get("provider")
    units = weather.get("units") or daily.get("units") or {}
    snow_unit = units.get("snowfall_sum")
    if not provider:
        warnings.append("legacy_weather_provider_unknown; historical fallback zeros cannot be identified")
    if not snow_unit:
        warnings.append("snowfall_unit_unknown")
    rows = []
    for index, day in enumerate(window_dates(start)):
        source_index = lookup.get(day)
        row = {"day_index": index, "date": day}
        for field in WEATHER_FIELDS:
            values = daily.get(field)
            value = number(values[source_index]) if values is not None and source_index is not None else None
            if value is not None and (value in {-999, -9999} or field in {"rain_sum", "snowfall_sum"} and value < 0):
                value = None
            if field == "snowfall_sum" and provider and "nasa" in str(provider).casefold():
                value = None
            row[field] = value
        rows.append(row)
    means = dict(zip(WEATHER_FIELDS, ("mean_daily_rainfall_mm", "mean_daily_snowfall",
                                     "mean_daily_max_temperature_c", "mean_daily_min_temperature_c")))

    def summarize(days):
        result = {"valid_days": {}}
        for field, output in means.items():
            values = [day[field] for day in days if day[field] is not None]
            result["valid_days"][field] = len(values)
            result[output] = round(sum(values) / len(values), 4) if values else None
        return result

    return {
        "units": {"rain_sum": "mm", "snowfall_sum": snow_unit,
                  "temperature_2m_max": "degC", "temperature_2m_min": "degC"},
        "pre_event_summary": summarize(rows[:10]),
        "post_event_summary": summarize(rows[11:]),
        "daily_20_days_series": rows,
    }


# Recovers source categories from saved search provenance, not from a publisher's nationality.
def source_group(article, news):
    source = str(article.get("source") or "").strip()
    if source.casefold().startswith("duckduckgo"):
        return "fallback_sources", "DuckDuckGo"
    metadata = news.get("search_metadata") or {}
    groups = {}
    for group in ("global_sources", "regional_sources", "fallback_sources"):
        for name in metadata.get(group + "_queried", []) or []:
            groups[str(name)] = group
    matches = [(name, group) for name, group in groups.items()
               if source.casefold() == name.casefold() or source.casefold().startswith(name.casefold() + " (")]
    if len(matches) == 1:
        name, group = matches[0]
        return group, name
    matches = [(name, groups[name]) for name, result in (news.get("source_results") or {}).items()
               if name in groups and any(item.get("url") == article.get("url")
                                        for item in result.get("articles", []))]
    if len({group for _, group in matches}) == 1 and matches:
        name, group = sorted(matches)[0]
        return group, name
    return None, source


# Publishes only the saved final articles' URLs and relevance fields, never their text.
def news_fields(record, warnings):
    news = (record or {}).get("news_data")
    if not isinstance(news, dict) or not isinstance(news.get("articles"), list):
        warnings.append("news_unavailable")
        return None
    deduped = {}
    for article in news["articles"]:
        if not isinstance(article, dict):
            raise ValueError("Malformed final article")
        url = str(article.get("url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            warnings.append("article_without_public_url_excluded")
            continue
        previous = deduped.get(url)
        if previous is None or (number(article.get("relevance_score")) or 0) > (number(previous.get("relevance_score")) or 0):
            deduped[url] = article
    counts = {group: Counter() for group in ("global_sources", "regional_sources", "fallback_sources")}
    output = []
    for url, article in deduped.items():
        group, source = source_group(article, news)
        if group:
            counts[group][source] += 1
        else:
            warnings.append("article_source_category_unresolved: " + source)
        output.append({
            "source": article.get("source"), "url": url,
            "relevance_score": number(article.get("relevance_score")),
            "relevance_reasons": article.get("relevance_reasons"),
            "relevance_penalties": article.get("relevance_penalties"),
        })
    metadata = {group: [{"source": name, "article_count": count} for name, count in sorted(values.items())]
                for group, values in counts.items()}
    metadata.update(total_articles_retrieved=len(output), filtered_final_articles=output)
    return {"search_metadata": metadata}


# Retains accepted causal steps and their audit fields through an explicit public allowlist.
def causal_fields(row):
    payload = json.loads(row.get("causal_chain_json") or row.get("causal_chain"))
    chain = payload["causal_chain"] if isinstance(payload, dict) else payload
    result = {key: row.get(key) for key in CAUSAL_TEXT}
    result.update({key: number(row.get(key), count=True) for key in CAUSAL_NUMBERS})
    result["causal_chain_length"] = len(chain)
    result["steps"] = [{key: step[key] for key in ("n_event", "type_event", "description", "supporting_quote")}
                       for step in chain]
    return result


# Exports only the four agreed summary fields without accepting rejected generated text.
def summary_fields(row, warnings):
    row = row or {}
    accepted = row.get("summary_validation_status") == "accepted"
    text = row.get("event_summary") if accepted else None
    if not accepted or missing(text) or text == "INSUFFICIENT_INFORMATION":
        warnings.append("accepted_summary_unavailable")
        text = None
    return {"usable_news": number(row.get("usable_news_count"), count=True),
            "total_chars": number(row.get("news_total_chars"), count=True),
            "news_input_quality": row.get("news_input_quality"), "event_summary": text}
