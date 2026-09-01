from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import pandas as pd

from support.constants import RESULTS_DIR
from support.event_news_summary import clean_text, response_json_text
from support.reasoner import DEFAULT_REASONER_MODEL, Reasoner


LOCAL_TERMS_PROMPT_VERSION = "local_news_search_terms_v5"
DEFAULT_INPUT_JSON = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "final_environmental_causal_dataset_2014_plus_news.json"
)
DEFAULT_OUTPUT_JSON = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "local_news_search_terms_llm70b.json"
)
DEFAULT_OUTPUT_CSV = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "local_news_search_terms_llm70b.csv"
)
DEFAULT_GROUPS_CSV = (
    Path(RESULTS_DIR)
    / "news_reasoning"
    / "local_news_search_term_groups_without_news.csv"
)

LOCAL_TERMS_PROMPT_TEMPLATE = """You are building a high-precision local-language news search dictionary for disaster-event retrieval.

Task:
For each country/disaster_type pair, generate short search terms that local newspapers in that country would realistically use for this exact disaster type.

Rules:
1. Return ONLY raw, valid JSON. Do not include explanations, greetings, markdown, or code fences.
2. Keep "country", "disaster_type", and "disaster_type_key" exactly as provided in the input list.
3. Use the main language or languages commonly used by local news outlets in the country.
4. For non-Latin scripts, use the native script, not romanization or pinyin, unless local media commonly use the romanized form.
5. Focus on disaster keywords, not country names, city names, dates, or full article titles.
6. Prefer common journalistic terms over technical jargon.
7. Return 3 to 8 distinct terms per country/disaster_type pair.
8. Do not repeat the same word or produce malformed/repetitive text.
9. Avoid terms that are too generic by themselves, such as "disaster", "water", "accident", "event", or "heavy".
10. EM-DAT disaster types "Road", "Rail", "Air", and "Water" are transport accidents. For "Water", use boat/ferry/ship accident or shipwreck terms, NOT flood terms.
11. For transport accidents, include the transport mode in the term, e.g. "road accident", "rail derailment", "plane crash", "boat accident", or their local-language equivalents.
12. For "Mass movement (wet)", use landslide/mudslide/debris-flow terms, not generic movement or flood terms.
13. Do not produce words in a language or script unless you are confident they are real terms used in news writing. If unsure, omit that country/disaster_type pair.
14. If the best local news language is French, Spanish, Portuguese, English, or Arabic for that country, it is acceptable to use that language.
15. Keep each term short enough to work in Google News or DuckDuckGo queries.
16. Preserve accents and local spelling when they are normally used.
17. It is better to omit a difficult country/disaster_type pair than to invent malformed local-language words.
18. Do not use generic weather or water words by themselves, e.g. "rain", "water", "river", "maji", "pula", or "imvura".

Good examples:
- China / Road: ["交通事故", "车祸", "道路交通事故", "撞车"]
- Greece / Water: ["ναυάγιο", "θαλάσσιο ατύχημα", "ατύχημα πλοίου"]
- India / Mass movement (wet): ["भूस्खलन", "चट्टान खिसकना", "मिट्टी धंसना"]
- Viet Nam / Flood: ["lũ lụt", "ngập lụt", "lũ quét", "nước lũ"]
- Peru / Flood: ["inundación", "inundaciones", "desborde", "huaico"]

Required JSON format:
{{
  "local_terms": [
    {{
      "country": "Peru",
      "disaster_type": "Flood",
      "disaster_type_key": "flood",
      "primary_languages": ["Spanish"],
      "terms": ["inundacion", "inundaciones", "desborde", "huaico"]
    }}
  ]
}}

Country/disaster_type pairs:
{groups}
"""

GENERIC_SINGLE_TERMS = {
    "accident",
    "accidente",
    "acidente",
    "disaster",
    "desastre",
    "desastre natural",
    "catastrophe",
    "catastrophe naturelle",
    "water",
    "eau",
    "agua",
    "event",
    "evento",
    "heavy",
    "pesado",
    "bhaari",
    "movement",
    "mouvement",
    "mass movement",
    "natural disaster",
    "crime",
    "aparadh",
    "offence",
    "road",
    "water accident",
    "water disaster",
    "bencana",
    "bencana alam",
    "rain",
    "chuva",
    "pula",
    "metsi",
    "metsi ya pula",
    "metsi ya maji",
    "amazi",
    "imvura",
    "maji",
    "majimaji",
    "maji makubwa",
    "kifo cha gari",
    "chute",
    "chutes",
    "umusozi",
    "imvura y umusozi",
    "umuyaga",
    "umuyaga mubi",
    "imvura y umuyaga",
    "umuyaga wa mizigo",
    "ikibazo cy umuhanda",
    "derailmento",
    "inundacaes",
}

BAD_TERM_KEYS = {
    "avariia na doroge",
    "dorozhno transportnyi proisshestvie",
    "avtoavariia",
    "dorozhnyi incident",
    "hariga",
    "taaridh",
    "avariya",
    "potamoploio",
    "kataklismos",
    "parathalassa",
    "padkarama",
}

DISASTER_DENY_TERMS = {
    "flood": {
        "landslide",
        "mudslide",
        "tanah longsor",
        "glissement de terrain",
        "deslizamiento de tierra",
        "山体滑坡",
        "泥石流",
        "भूस्खलन",
    },
    "mass movement": {
        "flood",
        "flooding",
        "inundation",
        "inundación",
        "inundacion",
        "inundação",
        "inundacao",
        "banjir",
        "inondation",
        "crue",
        "洪水",
        "水灾",
        "lũ lụt",
        "ngập lụt",
    },
    "water": {
        "flood",
        "flooding",
        "inundation",
        "inundación",
        "inundacion",
        "inundação",
        "inundacao",
        "inondation",
        "inondations",
        "débordement",
        "debordement",
        "crue",
        "water",
        "eau",
        "agua",
        "νερό",
        "πλημμύρα",
        "κατακλυσμός",
        "banjir",
        "air bah",
        "hujan",
        "badai",
        "maji",
        "mafuriko",
        "洪水",
        "水灾",
        "淹水",
        "lũ lụt",
        "ngập lụt",
        "nước lũ",
    },
    "miscellaneous accident": {
        "crime",
        "aparadh",
        "aparadh ki ghatna",
    },
}

LATIN_NEWS_LANGUAGES = {
    "afrikaans",
    "english",
    "french",
    "indonesian",
    "italian",
    "portuguese",
    "spanish",
    "swahili",
    "turkish",
    "vietnamese",
    "wolof",
}

UNRELIABLE_NON_LATIN_LANGUAGES = {
    "armenian",
    "dzongkha",
    "khmer",
}

SCRIPT_ARTIFACT_CHARS = set("玠么")
SCRIPT_ARTIFACT_FRAGMENTS = {
    "الطرريرا",
    "حوادثه",
    "حوادثة",
    "تنجد",
    "جادنا",
    "विपत्र",
}


# Loads the event-news dataset saved by the news retrieval pipeline.
def read_dataset(path: Path) -> List[Tuple[str, Dict[str, Any]]]:
    if not path.exists():
        raise SystemExit(f"Input JSON not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, Mapping):
        raise SystemExit("Input JSON must contain an object keyed by event id")

    records: List[Tuple[str, Dict[str, Any]]] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        event_id = clean_text(value.get("disaster_id")) or str(key)
        record = dict(value)
        record.setdefault("disaster_id", event_id)
        records.append((event_id, record))
    return records


# Converts a text label into a stable lowercase lookup key.
def lookup_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Normalizes a term only for duplicate and quality checks.
def term_quality_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[\"'`´“”‘’]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;:|/-")
    return text


# Checks whether a term contains Latin letters.
def has_latin_letters(value: Any) -> bool:
    return any("LATIN" in unicodedata.name(char, "") for char in str(value or ""))


# Checks whether a term contains letters from non-Latin scripts.
def has_non_latin_letters(value: Any) -> bool:
    return any(
        char.isalpha() and "LATIN" not in unicodedata.name(char, "")
        for char in str(value or "")
    )


# Detects whether the listed languages normally allow Latin-script search terms.
def allows_latin_terms(languages: Iterable[str]) -> bool:
    keys = {lookup_key(language) for language in languages if clean_text(language)}
    return not keys or bool(keys & LATIN_NEWS_LANGUAGES)


# Detects languages where the model produced malformed native-script terms in tests.
def has_unreliable_native_script(languages: Iterable[str]) -> bool:
    keys = {lookup_key(language) for language in languages if clean_text(language)}
    return bool(keys & UNRELIABLE_NON_LATIN_LANGUAGES)


# Detects malformed terms with the same token repeated several times.
def has_repeated_token_pattern(term: str) -> bool:
    tokens = term_quality_key(term).split()
    if len(tokens) < 3:
        return False
    if any(left == right for left, right in zip(tokens, tokens[1:])):
        return True
    if any(
        len(left) >= 4 and len(right) >= 4 and (left.startswith(right) or right.startswith(left))
        for left, right in zip(tokens, tokens[1:])
    ):
        return True
    if max(tokens.count(token) for token in set(tokens)) >= 3:
        return True

    pairs = [" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1)]
    return bool(pairs and max(pairs.count(pair) for pair in set(pairs)) >= 2)


# Detects malformed terms with the same character repeated too many times.
def has_repeated_character_pattern(term: str) -> bool:
    return bool(re.search(r"([^\W\d_])\1{2,}", unicodedata.normalize("NFKC", term)))


# Checks whether a term clearly belongs to a different disaster class.
def has_wrong_disaster_meaning(term: str, disaster_key: str) -> bool:
    denied = DISASTER_DENY_TERMS.get(lookup_key(disaster_key), set())
    term_key = term_quality_key(term)
    term_lookup = lookup_key(term)
    return any(
        denied_term in term_key or denied_term in term_lookup
        for denied_term in denied
    )


# Keeps only distinct and usable local search terms.
def clean_local_terms(
    terms: Iterable[Any],
    *,
    disaster_key: str,
    languages: Iterable[str],
) -> List[str]:
    cleaned_terms = []
    seen = set()
    latin_allowed = allows_latin_terms(languages)
    unreliable_native_script = has_unreliable_native_script(languages)

    for term in terms:
        cleaned = clean_text(term)
        key = term_quality_key(cleaned)
        if not cleaned or key in seen:
            continue
        if has_non_latin_letters(cleaned) and unreliable_native_script:
            continue
        if any(char in cleaned for char in SCRIPT_ARTIFACT_CHARS):
            continue
        if any(fragment in key for fragment in SCRIPT_ARTIFACT_FRAGMENTS):
            continue
        if len(cleaned) > 80 or has_repeated_token_pattern(cleaned):
            continue
        if has_repeated_character_pattern(cleaned):
            continue
        if lookup_key(cleaned) in GENERIC_SINGLE_TERMS:
            continue
        if lookup_key(cleaned) in BAD_TERM_KEYS:
            continue
        if has_latin_letters(cleaned) and has_non_latin_letters(cleaned):
            continue
        if has_latin_letters(cleaned) and not latin_allowed:
            continue
        if has_wrong_disaster_meaning(cleaned, disaster_key):
            continue
        seen.add(key)
        cleaned_terms.append(cleaned)

    return cleaned_terms


# Normalizes the optional language list returned by the LLM.
def clean_language_list(value: Any) -> List[str]:
    if isinstance(value, str):
        pieces = re.split(r"[,;|/]+", value)
    elif isinstance(value, IterableABC):
        pieces = list(value)
    else:
        pieces = []
    return [clean_text(piece) for piece in pieces if clean_text(piece)]


# Removes parenthetical EM-DAT qualifiers from disaster types.
def disaster_type_key(disaster_type: Any) -> str:
    return lookup_key(str(disaster_type or "").split("(")[0])


# Returns the number of retrieved articles already stored for one event.
def article_count(record: Mapping[str, Any]) -> int:
    metadata = (record.get("news_data") or {}).get("search_metadata") or {}
    try:
        return int(metadata.get("total_articles_retrieved") or 0)
    except (TypeError, ValueError):
        return 0


# Groups missing-news events by country and disaster type.
def country_disaster_groups(
    records: Iterable[Tuple[str, Mapping[str, Any]]],
    *,
    only_without_news: bool,
) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for event_id, record in records:
        if only_without_news and article_count(record) > 0:
            continue

        country = clean_text(record.get("country") or record.get("Country"))
        disaster_type = clean_text(record.get("disaster_type") or record.get("Disaster Type"))
        if not country or not disaster_type:
            continue

        key = (lookup_key(country), disaster_type_key(disaster_type))
        if key not in grouped:
            grouped[key] = {
                "country": country,
                "disaster_type": disaster_type,
                "disaster_type_key": key[1],
                "event_count": 0,
                "example_event_id": event_id,
                "example_location": clean_text(
                    record.get("emdat_location")
                    or (record.get("location_context") or {}).get("emdat_location")
                    or record.get("location")
                ),
            }
        grouped[key]["event_count"] += 1

    return sorted(
        grouped.values(),
        key=lambda item: (-int(item["event_count"]), item["country"], item["disaster_type"]),
    )


# Reads an existing local-terms JSON so the script can resume safely.
def read_existing_terms(path: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        return {}

    existing: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in payload.get("local_terms") or []:
        if not isinstance(item, Mapping):
            continue
        country = clean_text(item.get("country"))
        key = clean_text(item.get("disaster_type_key")) or disaster_type_key(
            item.get("disaster_type")
        )
        if not country or not key:
            continue
        existing[(lookup_key(country), lookup_key(key))] = dict(item)
    return existing


# Formats one batch of groups for the LLM prompt.
def format_groups(groups: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for index, group in enumerate(groups, start=1):
        location = clean_text(group.get("example_location")) or "unavailable"
        lines.append(
            f'{index}. country: {group["country"]} | '
            f'disaster_type: {group["disaster_type"]} | '
            f'disaster_type_key: {group["disaster_type_key"]} | '
            f'event_count: {group["event_count"]} | '
            f'example_location: {location}'
        )
    return "\n".join(lines)


# Builds the prompt for one LLM call.
def build_local_terms_prompt(groups: Iterable[Mapping[str, Any]]) -> str:
    return LOCAL_TERMS_PROMPT_TEMPLATE.format(groups=format_groups(groups))


# Parses and validates the JSON object returned by the LLM.
def parse_local_terms_response(response: Any) -> Tuple[List[Dict[str, Any]], str]:
    json_text = response_json_text(response)
    if not json_text:
        return [], "invalid_json"

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return [], "invalid_json"

    items = payload.get("local_terms")
    if not isinstance(items, list):
        return [], "missing_local_terms"

    rows = []
    dropped = 0
    for item in items:
        if not isinstance(item, Mapping):
            dropped += 1
            continue
        country = clean_text(item.get("country"))
        disaster_type = clean_text(item.get("disaster_type"))
        key = clean_text(item.get("disaster_type_key")) or disaster_type_key(disaster_type)
        languages = clean_language_list(item.get("primary_languages"))
        terms = clean_local_terms(
            item.get("terms") or [],
            disaster_key=key,
            languages=languages,
        )
        if not country or not disaster_type or not key or len(terms) < 3:
            dropped += 1
            continue
        rows.append(
            {
                "country": country,
                "disaster_type": disaster_type,
                "disaster_type_key": lookup_key(key),
                "primary_languages": languages,
                "terms": list(dict.fromkeys(terms)),
            }
        )

    if not rows:
        return [], "empty_terms"
    if dropped:
        return rows, "parsed_with_dropped_items"
    return rows, "parsed"


# Writes both the JSON dictionary and a readable CSV copy.
def write_outputs(
    terms_by_key: Mapping[Tuple[str, str], Mapping[str, Any]],
    *,
    output_json: Path,
    output_csv: Path,
    groups_csv: Path,
    groups: Iterable[Mapping[str, Any]],
    model_name: str,
) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    sorted_terms = sorted(
        (dict(value) for value in terms_by_key.values()),
        key=lambda item: (item.get("country", ""), item.get("disaster_type_key", "")),
    )
    payload = {
        "prompt_version": LOCAL_TERMS_PROMPT_VERSION,
        "model_name": model_name,
        "local_terms": sorted_terms,
    }
    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    csv_rows = []
    for item in sorted_terms:
        csv_rows.append(
            {
                "country": item.get("country", ""),
                "disaster_type": item.get("disaster_type", ""),
                "disaster_type_key": item.get("disaster_type_key", ""),
                "primary_languages": " | ".join(item.get("primary_languages") or []),
                "terms": " | ".join(item.get("terms") or []),
            }
        )
    pd.DataFrame(csv_rows).to_csv(output_csv, index=False)
    pd.DataFrame(list(groups)).to_csv(groups_csv, index=False)


# Defines the command-line options for local search-term generation.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate local-language disaster search terms for missing-news events."
    )
    parser.add_argument("--input-json", default=str(DEFAULT_INPUT_JSON))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--groups-csv", default=str(DEFAULT_GROUPS_CSV))
    parser.add_argument("--model-name", default=DEFAULT_REASONER_MODEL)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--groups-per-call", type=int, default=20)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--only-without-news", action="store_true")
    parser.add_argument("--load-in-4bit", action="store_true")
    parser.add_argument("--load-in-8bit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


# Coordinates grouping, LLM calls, validation, and output writing.
def main() -> None:
    args = parse_args()
    input_json = Path(args.input_json)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    groups_csv = Path(args.groups_csv)

    records = read_dataset(input_json)
    groups = country_disaster_groups(
        records,
        only_without_news=args.only_without_news,
    )

    existing = {} if args.force else read_existing_terms(output_json)
    pending = [
        group
        for group in groups
        if (lookup_key(group["country"]), lookup_key(group["disaster_type_key"]))
        not in existing
    ]
    selected = pending[args.offset:] if args.offset else pending
    if args.limit:
        selected = selected[: args.limit]

    print(f"Input events: {len(records)}")
    print(f"Country/disaster groups: {len(groups)}")
    print(f"Existing term groups: {len(existing)}")
    print(f"Selected groups: {len(selected)}")
    print(f"Only without news: {args.only_without_news}")
    print(f"Dry run: {args.dry_run}")
    print(f"Output JSON: {output_json}")
    print(f"Output CSV: {output_csv}")
    print(f"Groups CSV: {groups_csv}")

    if args.dry_run:
        print()
        print("First prompt:")
        print(build_local_terms_prompt(selected[: args.groups_per_call]))
        write_outputs(
            existing,
            output_json=output_json,
            output_csv=output_csv,
            groups_csv=groups_csv,
            groups=groups,
            model_name=args.model_name,
        )
        return

    reasoner = Reasoner(
        model_name=args.model_name,
        load_in_4bit=args.load_in_4bit,
        load_in_8bit=args.load_in_8bit,
        max_new_tokens=args.max_new_tokens,
    )

    terms_by_key = dict(existing)
    processed_groups = 0
    parsed_groups = 0

    for start in range(0, len(selected), args.groups_per_call):
        batch = selected[start : start + args.groups_per_call]
        print(
            f"[{start + 1}/{len(selected)}] "
            f"groups in this LLM call: {len(batch)}",
            flush=True,
        )
        prompt = build_local_terms_prompt(batch)
        raw_response = reasoner.ask(prompt, max_new_tokens=args.max_new_tokens)
        parsed_rows, parse_status = parse_local_terms_response(raw_response)
        print(f"parse_status: {parse_status}; parsed rows: {len(parsed_rows)}", flush=True)

        for row in parsed_rows:
            key = (lookup_key(row["country"]), lookup_key(row["disaster_type_key"]))
            terms_by_key[key] = row
        processed_groups += len(batch)
        parsed_groups += len(parsed_rows)

        write_outputs(
            terms_by_key,
            output_json=output_json,
            output_csv=output_csv,
            groups_csv=groups_csv,
            groups=groups,
            model_name=args.model_name,
        )

    print()
    print(f"Processed groups: {processed_groups}")
    print(f"Parsed term groups: {parsed_groups}")
    print(f"Total term groups in output: {len(terms_by_key)}")


if __name__ == "__main__":
    main()
