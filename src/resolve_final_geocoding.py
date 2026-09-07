from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import pandas as pd

from prepare_llm_location_nominatim_queries import event_candidates
from support.constants import EMDAT_INPUT_PATH, RECENT_EMDAT_ADM2_AOIS_CSV


DEFAULT_OUTPUT_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_POSITIONS_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2_positions.csv"
)
DEFAULT_SUMMARY_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2_position_summary.csv"
)
DEFAULT_LLM_CSV = (
    "results/recent_emdat_geocoding/llm_location_cleaning_new_prompt_all_70b_from_2014-04-03.csv"
)
DEFAULT_NOMINATIM_RESULTS_CSV = (
    "results/recent_emdat_geocoding/llm_location_nominatim_results_new_prompt_all_70b_from_2014-04-03.csv"
)
DEFAULT_REVIEW_AUDIT_CSV = (
    "results/recent_emdat_geocoding/llm70b_nominatim_review_resolution_audit_v2_from_2014-04-03.csv"
)
DEFAULT_REVIEW_SUMMARY_CSV = (
    "results/recent_emdat_geocoding/llm70b_nominatim_review_resolution_summary_v2_from_2014-04-03.csv"
)
EVENT_ID_COLUMN = "DisNo."
QUERY_ID_COLUMN = "geocoding_query_id"

ADMIN_OR_AREA_TYPES = {
    "administrative",
    "state",
    "province",
    "region",
    "county",
    "district",
    "municipality",
    "territory",
    "prefecture",
    "governorate",
    "department",
}
POPULATED_OR_LOCAL_TYPES = {
    "city",
    "town",
    "village",
    "hamlet",
    "suburb",
    "neighbourhood",
    "quarter",
    "borough",
    "residential",
    "locality",
    "township",
}
NATURAL_TYPES = {
    "river",
    "island",
    "archipelago",
    "lake",
    "bay",
    "gulf",
    "strait",
    "water",
    "reservoir",
    "valley",
    "mountain",
    "peak",
    "volcano",
    "peninsula",
    "desert",
}
HISTORIC_REGION_TYPES = {"historic"}
POI_OR_NON_GEOGRAPHIC_TYPES = {
    "fuel",
    "government",
    "hotel",
    "bank",
    "cafe",
    "restaurant",
    "school",
    "university",
    "hospital",
    "bus_stop",
    "station",
    "cemetery",
    "attraction",
    "bridge",
    "office",
    "retail",
    "supermarket",
    "industrial",
    "commercial",
    "house",
    "building",
}
ADMIN_WORDS = {
    "province",
    "state",
    "region",
    "district",
    "county",
    "department",
    "governorate",
    "prefecture",
    "territory",
    "oblast",
    "municipality",
    "kanto",
    "kanton",
}


# Reads tabular data from Excel or CSV.
def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path, low_memory=False)


# Returns a trimmed text value suitable for comparisons and output.
def clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "[]"}:
        return ""
    return " ".join(text.replace("\n", " ").replace("\r", " ").split())


# Builds a real start date from EM-DAT date columns.
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


# Loads the EM-DAT rows included in the release perimeter.
def load_emdat_events(path: Path, start_date: str) -> pd.DataFrame:
    frame = read_table(path)
    if EVENT_ID_COLUMN not in frame.columns:
        raise SystemExit(f"EM-DAT file missing column: {EVENT_ID_COLUMN}")
    dates = build_start_date(frame)
    frame = frame[dates.ge(pd.Timestamp(start_date))].copy()
    frame["_llm_start_date"] = dates.loc[frame.index].dt.strftime("%Y-%m-%d")
    return frame.reset_index(drop=True)


# Converts numeric fields while preserving missing values.
def as_number(value: Any) -> Optional[float]:
    try:
        number = pd.to_numeric(value, errors="coerce")
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return float(number)


# Normalizes text for broad equality checks.
def normalized_text(value: Any) -> str:
    return clean_text(value).casefold().replace("'", "").replace("’", "")


# Returns whether the original EM-DAT location was empty.
def empty_original_location(row: Mapping[str, Any]) -> bool:
    return not clean_text(row.get("Location", ""))


# Returns whether the LLM query is just the country fallback.
def is_country_fallback(row: Mapping[str, Any], emdat_row: Mapping[str, Any]) -> bool:
    query = normalized_text(row.get("llm_geocoding_string") or row.get("geocoding_query"))
    country = normalized_text(emdat_row.get("Country", ""))
    return bool(query and country and query == country and empty_original_location(emdat_row))


# Checks if a broad administrative query returned an implausibly tiny area.
def has_small_admin_bbox(row: Mapping[str, Any]) -> bool:
    width = as_number(row.get("bbox_width_deg"))
    height = as_number(row.get("bbox_height_deg"))
    query = normalized_text(row.get("llm_geocoding_string") or row.get("geocoding_query"))
    if width is None or height is None:
        return False
    if not any(word in query for word in ADMIN_WORDS):
        return False
    return max(width, height) < 0.05


# Classifies a Nominatim review result using place type and bounding-box signals.
def classify_review_result(
    row: Mapping[str, Any],
    emdat_row: Mapping[str, Any],
) -> Tuple[str, str]:
    if as_number(row.get("latitude")) is None or as_number(row.get("longitude")) is None:
        return "reject", "missing_coordinates"

    place_type = clean_text(row.get("place_type")).casefold()
    if is_country_fallback(row, emdat_row):
        return "accept", "country_fallback_empty_location"
    if place_type in POI_OR_NON_GEOGRAPHIC_TYPES:
        return "reject", "poi_or_non_geographic_type"
    if place_type in ADMIN_OR_AREA_TYPES:
        if has_small_admin_bbox(row):
            return "reject", "small_bbox_for_admin_query"
        return "accept", "admin_or_area_result"
    if place_type in POPULATED_OR_LOCAL_TYPES:
        return "accept", "populated_or_local_place"
    if place_type in NATURAL_TYPES:
        return "accept", "natural_geographic_feature"
    if place_type in HISTORIC_REGION_TYPES:
        return "accept", "historic_region_like_result"
    return "reject", "unsupported_place_type"


# Keeps the latest geocoding result for each query id.
def latest_nominatim_results(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty or QUERY_ID_COLUMN not in results.columns:
        return results.copy()
    return results.drop_duplicates(QUERY_ID_COLUMN, keep="last").copy()


# Builds one best Nominatim row for each EM-DAT event.
def event_level_nominatim(llm: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    candidates = event_candidates(llm)
    if candidates.empty:
        return pd.DataFrame(columns=[EVENT_ID_COLUMN])

    latest = latest_nominatim_results(results).rename(
        columns={"countrycodes": "countrycodes_nominatim"}
    )
    merged = candidates.merge(latest, on=QUERY_ID_COLUMN, how="left")
    status_rank = {"matched": 0, "matched_review": 1, "no_result": 2, "error": 3}
    merged["_status_rank"] = merged["geocoding_status"].map(status_rank).fillna(4)
    merged["_result_score"] = pd.to_numeric(
        merged.get("result_score", pd.Series(index=merged.index)),
        errors="coerce",
    ).fillna(-9999)
    merged = merged.sort_values(
        [EVENT_ID_COLUMN, "_status_rank", "_result_score"],
        ascending=[True, True, False],
        kind="stable",
    )
    return merged.drop_duplicates(EVENT_ID_COLUMN, keep="first").copy()


# Builds one ADM/GADM fallback position for each event with matched ADM2 AOIs.
def load_adm_positions(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    frame = pd.read_csv(path, low_memory=False)
    required = {"emdat_disaster_id", "bbox_status", "centroid_lat", "centroid_lon"}
    if not required.issubset(frame.columns):
        return {}

    matched = frame[frame["bbox_status"].eq("matched")].copy()
    matched["centroid_lat"] = pd.to_numeric(matched["centroid_lat"], errors="coerce")
    matched["centroid_lon"] = pd.to_numeric(matched["centroid_lon"], errors="coerce")
    matched = matched.dropna(subset=["centroid_lat", "centroid_lon"])

    output: Dict[str, Dict[str, Any]] = {}
    for event_id, group in matched.groupby("emdat_disaster_id", sort=False):
        output[str(event_id)] = {
            "latitude": float(group["centroid_lat"].mean()),
            "longitude": float(group["centroid_lon"].mean()),
            "adm_gadm_aoi_count": len(group),
            "adm_gadm_bbox_quality": " | ".join(
                sorted(set(group.get("bbox_quality", pd.Series(dtype=str)).dropna().astype(str)))
            ),
        }
    return output


# Returns the final coordinate source for one event.
def resolve_event_position(
    emdat_row: Mapping[str, Any],
    adm_positions: Mapping[str, Dict[str, Any]],
    nominatim_by_event: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    event_id = clean_text(emdat_row.get(EVENT_ID_COLUMN))
    direct_lat = as_number(emdat_row.get("Latitude"))
    direct_lon = as_number(emdat_row.get("Longitude"))
    if direct_lat is not None and direct_lon is not None:
        return {
            "position_source": "em-dat",
            "latitude": direct_lat,
            "longitude": direct_lon,
            "geocoding_final_bucket": "accepted_coordinates",
        }

    adm_position = adm_positions.get(event_id)
    if adm_position:
        return {
            "position_source": "ADM/GADM",
            "latitude": adm_position["latitude"],
            "longitude": adm_position["longitude"],
            "geocoding_final_bucket": "accepted_coordinates",
            "adm_gadm_aoi_count": adm_position.get("adm_gadm_aoi_count", ""),
            "adm_gadm_bbox_quality": adm_position.get("adm_gadm_bbox_quality", ""),
        }

    nominatim_row = nominatim_by_event.get(event_id)
    if nominatim_row:
        status = clean_text(nominatim_row.get("geocoding_status"))
        lat = as_number(nominatim_row.get("latitude"))
        lon = as_number(nominatim_row.get("longitude"))
        if status == "matched" and lat is not None and lon is not None:
            return {
                "position_source": "llm_nominatim",
                "latitude": lat,
                "longitude": lon,
                "geocoding_final_bucket": "accepted_coordinates",
            }
        if status == "matched_review":
            decision, bucket = classify_review_result(nominatim_row, emdat_row)
            if decision == "accept" and lat is not None and lon is not None:
                return {
                    "position_source": "llm_nominatim_review_accepted",
                    "latitude": lat,
                    "longitude": lon,
                    "geocoding_final_bucket": "accepted_coordinates",
                    "review_decision": decision,
                    "review_bucket": bucket,
                }
            return {
                "position_source": "not_recovered",
                "latitude": "",
                "longitude": "",
                "geocoding_final_bucket": "not_recovered",
                "review_decision": decision,
                "review_bucket": bucket,
            }

    return {
        "position_source": "not_recovered",
        "latitude": "",
        "longitude": "",
        "geocoding_final_bucket": "not_recovered",
    }


# Copies useful Nominatim metadata into the final audit columns.
def nominatim_metadata(row: Mapping[str, Any]) -> Dict[str, Any]:
    if not row:
        return {}
    return {
        "llm_geocoding_string": row.get("llm_geocoding_string", ""),
        "nominatim_status": row.get("geocoding_status", ""),
        "nominatim_display_name": row.get("display_name", ""),
        "nominatim_place_type": row.get("place_type", ""),
        "nominatim_result_quality": row.get("result_quality", ""),
        "nominatim_result_quality_reasons": row.get("result_quality_reasons", ""),
        "nominatim_bbox_south": row.get("bbox_south", ""),
        "nominatim_bbox_north": row.get("bbox_north", ""),
        "nominatim_bbox_west": row.get("bbox_west", ""),
        "nominatim_bbox_east": row.get("bbox_east", ""),
        "nominatim_bbox_width_deg": row.get("bbox_width_deg", ""),
        "nominatim_bbox_height_deg": row.get("bbox_height_deg", ""),
    }


# Creates the final geocoding outputs from EM-DAT, ADM/GADM, LLM, and Nominatim.
def build_outputs(
    emdat: pd.DataFrame,
    llm: pd.DataFrame,
    nominatim_results: pd.DataFrame,
    adm_positions: Mapping[str, Dict[str, Any]],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    event_nominatim = event_level_nominatim(llm, nominatim_results)
    nominatim_by_event = {
        clean_text(row.get(EVENT_ID_COLUMN)): row
        for row in event_nominatim.to_dict("records")
        if clean_text(row.get(EVENT_ID_COLUMN))
    }

    rows = []
    audit_rows = []
    for _, emdat_row in emdat.iterrows():
        event_id = clean_text(emdat_row.get(EVENT_ID_COLUMN))
        nominatim_row = nominatim_by_event.get(event_id, {})
        resolved = resolve_event_position(emdat_row, adm_positions, nominatim_by_event)
        output = emdat_row.to_dict()
        output.update(nominatim_metadata(nominatim_row))
        output.update(resolved)
        rows.append(output)

        if clean_text(nominatim_row.get("geocoding_status")) == "matched_review":
            decision, bucket = classify_review_result(nominatim_row, emdat_row)
            audit_rows.append(
                {
                    "event_id": event_id,
                    "country": emdat_row.get("Country", ""),
                    "location": emdat_row.get("Location", ""),
                    "llm_geocoding_string": nominatim_row.get("llm_geocoding_string", ""),
                    "geocoding_status": nominatim_row.get("geocoding_status", ""),
                    "display_name": nominatim_row.get("display_name", ""),
                    "place_type": nominatim_row.get("place_type", ""),
                    "bbox_width_deg": nominatim_row.get("bbox_width_deg", ""),
                    "bbox_height_deg": nominatim_row.get("bbox_height_deg", ""),
                    "review_decision": decision,
                    "review_bucket": bucket,
                }
            )

    full = pd.DataFrame(rows)
    positions = pd.DataFrame(
        {
            "event_id": full[EVENT_ID_COLUMN],
            "country": full.get("Country", ""),
            "disaster_type": full.get("Disaster Type", ""),
            "start_date": full.get("_llm_start_date", ""),
            "location": full.get("Location", ""),
            "position_source": full["position_source"],
            "latitude": full["latitude"],
            "longitude": full["longitude"],
        }
    )

    counts = full["position_source"].value_counts().rename_axis("position_source")
    summary = counts.reset_index(name="count")
    summary["percent_of_events"] = (summary["count"] / len(full) * 100).round(2)

    audit = pd.DataFrame(audit_rows)
    if audit.empty:
        audit_summary = pd.DataFrame(columns=["review_decision", "review_bucket", "count", "percent_of_review"])
    else:
        audit_summary = (
            audit.groupby(["review_decision", "review_bucket"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values(["review_decision", "count"], ascending=[True, False])
        )
        audit_summary["percent_of_review"] = (
            audit_summary["count"] / len(audit) * 100
        ).round(2)

    return full, positions, summary, audit, audit_summary


# Defines the command-line options for final geocoding resolution.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve the final event coordinate table from generated geocoding outputs."
    )
    parser.add_argument("--emdat-file", default=EMDAT_INPUT_PATH)
    parser.add_argument("--start-date", default="2014-04-03")
    parser.add_argument("--llm-csv", default=DEFAULT_LLM_CSV)
    parser.add_argument("--nominatim-results-csv", default=DEFAULT_NOMINATIM_RESULTS_CSV)
    parser.add_argument("--adm2-aois-csv", default=RECENT_EMDAT_ADM2_AOIS_CSV)
    parser.add_argument("--output-csv", default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--positions-csv", default=DEFAULT_POSITIONS_CSV)
    parser.add_argument("--summary-csv", default=DEFAULT_SUMMARY_CSV)
    parser.add_argument("--review-audit-csv", default=DEFAULT_REVIEW_AUDIT_CSV)
    parser.add_argument("--review-summary-csv", default=DEFAULT_REVIEW_SUMMARY_CSV)
    return parser.parse_args()


# Loads inputs, resolves final coordinates, and writes full/audit outputs.
def main() -> None:
    args = parse_args()
    emdat = load_emdat_events(Path(args.emdat_file), args.start_date)
    llm = read_table(Path(args.llm_csv))
    nominatim_results = read_table(Path(args.nominatim_results_csv))
    adm_positions = load_adm_positions(Path(args.adm2_aois_csv))

    full, positions, summary, audit, audit_summary = build_outputs(
        emdat,
        llm,
        nominatim_results,
        adm_positions,
    )

    outputs = [
        (Path(args.output_csv), full),
        (Path(args.positions_csv), positions),
        (Path(args.summary_csv), summary),
        (Path(args.review_audit_csv), audit),
        (Path(args.review_summary_csv), audit_summary),
    ]
    for path, frame in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)

    accepted = len(full[full["position_source"].ne("not_recovered")])
    print("rows:", len(full))
    print(summary.to_string(index=False))
    print()
    print(
        "final accepted coordinates: "
        f"{accepted} / {len(full)} = {round(accepted / len(full) * 100, 2) if len(full) else 0.0}%"
    )
    print("not recovered:", int(full["position_source"].eq("not_recovered").sum()))
    print()
    print("Resolved full CSV:", args.output_csv)
    print("Resolved positions CSV:", args.positions_csv)
    print("Resolved summary CSV:", args.summary_csv)
    print("Review audit CSV:", args.review_audit_csv)
    print("Review audit summary CSV:", args.review_summary_csv)


if __name__ == "__main__":
    main()
