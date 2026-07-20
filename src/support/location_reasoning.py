from __future__ import annotations

from typing import Mapping


UNKNOWN_LOCATION = "UNKNOWN"

LOCATION_CANONICALIZATION_TEMPLATE = """Extract the most specific canonical geographic name from the following descriptive location context, and format it perfectly for a Geocoding API.

Rules:
1. Format the output STRICTLY as: "Specific_Entity, Country". (e.g., "Miami, United States" or "Midwest, United States").
2. CRITICAL: If the Location is empty, overly generic, or essentially matches the Country/Region name (e.g. Location: "China", Country: "China"), output ONLY the Country name.
3. Remove all descriptive noise (e.g., "Villages near", "Slums of", "northern part of").
4. If Puerto Rico is mentioned, treat it as its own distinct country entity (output: "Puerto Rico").
5. Do not include any introductory words, conversational text, or punctuation outside the requested format.
6. If absolutely no recognizable geographical location can be extracted, output exactly the word "UNKNOWN".

Descriptive location context:
Country: {country}
Subregion: {subregion}
Region: {region}
Location: {location}

Geocoding string:"""


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
