import argparse
import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from support.constants import EMDAT_INPUT_PATH, RECENT_EMDAT_GEOCODING_OUTPUT_DIR
from support.location_reasoning import UNKNOWN_LOCATION, clean_field

try:
    import pycountry
except ImportError:  # pragma: no cover - exercised only when dependency is absent.
    pycountry = None


EVENT_ID_COLUMN = "DisNo."
DEFAULT_LLM_CSV = (
    Path(RECENT_EMDAT_GEOCODING_OUTPUT_DIR)
    / "llm_location_cleaning_all_checked_from_2014-04-03.csv"
)
DEFAULT_OUTPUT_CSV = (
    Path(RECENT_EMDAT_GEOCODING_OUTPUT_DIR)
    / "llm_location_nominatim_queries_for_geocoding_from_2014-04-03.csv"
)
WHITESPACE_RE = re.compile(r"\s+")
ISO3_TO_ISO2_OVERRIDES = {
    "XKX": "XK",
}


# Reads input data from Excel or CSV.
def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path)


# Builds a real start date from the EM-DAT date columns.
def build_start_date(frame: pd.DataFrame) -> pd.Series:
    required = {"Start Year", "Start Month", "Start Day"}
    if not required.issubset(frame.columns):
        return pd.Series(pd.NaT, index=frame.index)
    return pd.to_datetime(
        {
            "year": pd.to_numeric(frame["Start Year"], errors="coerce"),
            "month": pd.to_numeric(frame["Start Month"], errors="coerce"),
            "day": pd.to_numeric(frame["Start Day"], errors="coerce"),
        },
        errors="coerce",
    )


# Removes accents before creating stable normalized keys.
def strip_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char))


# Normalizes text so equivalent queries share the same key.
def normalized_key(value: str) -> str:
    text = strip_accents(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


# Creates a stable short id for a geocoding query.
def query_id(query_key: str) -> str:
    return hashlib.sha1(query_key.encode("utf-8")).hexdigest()[:12]


# Converts EM-DAT ISO3 country codes to Nominatim ISO2 countrycodes.
def iso3_to_iso2(value: object) -> str:
    iso3 = clean_field(value).upper()
    if not iso3:
        return ""
    if len(iso3) == 2:
        return iso3.lower()
    if iso3 in ISO3_TO_ISO2_OVERRIDES:
        return ISO3_TO_ISO2_OVERRIDES[iso3].lower()
    if pycountry is None:
        return ""
    country = pycountry.countries.get(alpha_3=iso3)
    return country.alpha_2.lower() if country else ""


# Loads the EM-DAT context needed to fill missing LLM output columns.
def emdat_context(path: Path, start_date: str) -> pd.DataFrame:
    emdat = read_table(path)
    if EVENT_ID_COLUMN not in emdat.columns:
        raise SystemExit(f"EM-DAT input missing column: {EVENT_ID_COLUMN}")

    rows = emdat.copy()
    if start_date:
        dates = build_start_date(rows)
        rows = rows[dates.ge(pd.Timestamp(start_date))].copy()

    keep = [EVENT_ID_COLUMN]
    for column in ["ISO", "Country", "Subregion", "Region", "Location"]:
        if column in rows.columns:
            keep.append(column)
    return rows[keep].drop_duplicates(EVENT_ID_COLUMN, keep="first")


# Converts LLM-cleaned locations into event-level geocoding candidates.
def event_candidates(llm: pd.DataFrame) -> pd.DataFrame:
    required = {EVENT_ID_COLUMN, "llm_canonical_location"}
    missing = required - set(llm.columns)
    if missing:
        raise SystemExit("LLM CSV missing column(s): " + ", ".join(sorted(missing)))

    rows = llm.copy()
    rows["llm_geocoding_string"] = rows["llm_canonical_location"].map(clean_field)
    unknown = rows["llm_geocoding_string"].str.upper().eq(UNKNOWN_LOCATION)
    rows = rows[rows["llm_geocoding_string"].ne("") & ~unknown].copy()

    if "ISO" not in rows.columns:
        rows["ISO"] = ""
    rows["countrycodes"] = rows["ISO"].map(iso3_to_iso2)

    query_key = rows.apply(
        lambda row: normalized_key(
            f"{row['llm_geocoding_string']}|{row.get('countrycodes', '')}"
        ),
        axis=1,
    )
    rows["geocoding_query_key"] = query_key
    rows["geocoding_query_id"] = rows["geocoding_query_key"].map(query_id)
    rows["query_quality"] = "usable"
    rows["query_quality_reasons"] = ""
    rows.loc[rows["countrycodes"].eq(""), "query_quality_reasons"] = "missing_countrycode"
    return rows


# Joins unique non-empty values while preserving their first occurrence.
def unique_join(values: pd.Series) -> str:
    seen: List[str] = []
    for value in values.dropna().astype(str):
        text = clean_field(value)
        if text and text not in seen:
            seen.append(text)
    return " | ".join(seen)


# Aggregates event candidates into unique Nominatim query rows.
def build_queries(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame(
            columns=[
                "query_rank",
                "geocoding_query_id",
                "geocoding_query",
                "geocoding_query_key",
                "query_quality",
            ]
        )

    rows: List[Dict] = []
    sort_columns = ["geocoding_query_key", EVENT_ID_COLUMN]
    for query_key, group in candidates.sort_values(sort_columns).groupby(
        "geocoding_query_key",
        sort=False,
    ):
        first = group.iloc[0]
        rows.append(
            {
                "geocoding_query_id": first["geocoding_query_id"],
                "geocoding_query": first["llm_geocoding_string"],
                "geocoding_query_key": query_key,
                "query_quality": "usable",
                "query_quality_reasons": unique_join(group["query_quality_reasons"]),
                "country": unique_join(group["Country"]) if "Country" in group.columns else "",
                "iso": unique_join(group["ISO"]) if "ISO" in group.columns else "",
                "countrycodes": first.get("countrycodes", ""),
                "place_name": first["llm_geocoding_string"],
                "suppress_country_variants": "true",
                "event_count": group[EVENT_ID_COLUMN].nunique(),
                "candidate_row_count": len(group),
                "event_ids": unique_join(group[EVENT_ID_COLUMN]),
            }
        )

    queries = pd.DataFrame(rows)
    queries = queries.sort_values(
        ["event_count", "geocoding_query"],
        ascending=[False, True],
    ).reset_index(drop=True)
    queries.insert(0, "query_rank", range(1, len(queries) + 1))
    return queries


# Builds a compact summary of generated candidates and query rows.
def build_summary(llm: pd.DataFrame, candidates: pd.DataFrame, queries: pd.DataFrame) -> pd.DataFrame:
    def count_missing_countrycodes(frame: pd.DataFrame) -> int:
        if frame.empty or "countrycodes" not in frame.columns:
            return 0
        return int(frame["countrycodes"].fillna("").astype(str).eq("").sum())

    rows = [
        {"metric": "llm_input_rows", "count": len(llm)},
        {"metric": "non_unknown_event_rows", "count": len(candidates)},
        {"metric": "unique_geocoding_queries", "count": len(queries)},
        {"metric": "event_rows_missing_countrycodes", "count": count_missing_countrycodes(candidates)},
        {"metric": "query_rows_missing_countrycodes", "count": count_missing_countrycodes(queries)},
    ]
    return pd.DataFrame(rows)


# Defines the command-line options for preparing Nominatim queries.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build Nominatim query rows from LLM-normalized EM-DAT location strings. "
            "The LLM string is used as q, while EM-DAT country context is kept "
            "separate through countrycodes when ISO information is available."
        )
    )
    parser.add_argument("--llm-csv", default=str(DEFAULT_LLM_CSV))
    parser.add_argument("--emdat-file", default=EMDAT_INPUT_PATH)
    parser.add_argument("--start-date", default="2014-04-03")
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--summary-csv", default="")
    return parser.parse_args()


# Prepares query and summary CSV files from the LLM location output.
def main() -> None:
    args = parse_args()
    llm = pd.read_csv(args.llm_csv)
    context = emdat_context(Path(args.emdat_file), args.start_date)

    if "ISO" not in llm.columns or "Country" not in llm.columns:
        llm = llm.merge(context, on=EVENT_ID_COLUMN, how="left", suffixes=("", "_emdat"))
        for column in ["ISO", "Country", "Subregion", "Region", "Location"]:
            emdat_column = f"{column}_emdat"
            if column not in llm.columns and emdat_column in llm.columns:
                llm[column] = llm[emdat_column]
            elif column in llm.columns and emdat_column in llm.columns:
                llm[column] = llm[column].where(llm[column].notna(), llm[emdat_column])
        llm = llm.drop(columns=[c for c in llm.columns if c.endswith("_emdat")])

    candidates = event_candidates(llm)
    queries = build_queries(candidates)
    summary = build_summary(llm, candidates, queries)

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    queries.to_csv(output_path, index=False)

    summary_path: Optional[Path] = None
    if args.summary_csv:
        summary_path = Path(args.summary_csv)
    else:
        summary_path = output_path.with_name(output_path.stem + "_summary.csv")
    summary.to_csv(summary_path, index=False)

    print(summary.to_string(index=False))
    print()
    print(f"Queries CSV: {output_path}")
    print(f"Summary CSV: {summary_path}")
    if pycountry is None:
        print()
        print("Warning: pycountry is not installed; countrycodes could not be derived from ISO3.")


if __name__ == "__main__":
    main()
