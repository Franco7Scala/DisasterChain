from __future__ import annotations

from typing import Mapping


UNKNOWN_LOCATION = "UNKNOWN"

LOCATION_CANONICALIZATION_TEMPLATE = """Extract the single most specific canonical city, district, or region name from the following descriptive location context.

Rules:
1. Return ONLY the geographical entity name. Do not include any introductory words, conversational text, or punctuation.
2. Prefer the most specific usable entity from Location. Use Country/Subregion/Region only as context or fallback.
3. If multiple locations are mentioned, extract the primary one or the broader district.
4. Remove all descriptive noise (e.g., "Villages near", "Slums of", "outskirts of", "northern part of").
5. If no recognizable geographical location can be extracted, output exactly the word "UNKNOWN".

Descriptive location context:
Country: {country}
Subregion: {subregion}
Region: {region}
Location: {location}

Canonical name:"""


def clean_field(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "[]"}:
        return ""
    return " ".join(text.replace("\n", " ").replace("\r", " ").split())


def build_location_canonicalization_prompt(
    *,
    country: object = "",
    subregion: object = "",
    region: object = "",
    location: object = "",
) -> str:
    return LOCATION_CANONICALIZATION_TEMPLATE.format(
        country=clean_field(country) or UNKNOWN_LOCATION,
        subregion=clean_field(subregion) or UNKNOWN_LOCATION,
        region=clean_field(region) or UNKNOWN_LOCATION,
        location=clean_field(location) or UNKNOWN_LOCATION,
    )


def build_location_prompt_from_row(row: Mapping[str, object]) -> str:
    return build_location_canonicalization_prompt(
        country=row.get("Country", ""),
        subregion=row.get("Subregion", ""),
        region=row.get("Region", ""),
        location=row.get("Location", ""),
    )


def normalize_canonical_location_response(response: object) -> str:
    text = clean_field(response)
    if not text:
        return UNKNOWN_LOCATION

    first_line = text.splitlines()[0].strip()
    first_line = first_line.strip("\"'` ")
    first_line = first_line.rstrip(".,;:")
    if first_line.upper() == UNKNOWN_LOCATION:
        return UNKNOWN_LOCATION
    return first_line or UNKNOWN_LOCATION
