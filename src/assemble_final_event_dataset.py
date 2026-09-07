from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd


DEFAULT_GEOCODING_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_BASE_CSV = "results/disasters_per_satellite.csv"
DEFAULT_WEATHER_PROGRESS_CSV = "results/weather/weather_2014_plus_progress.csv"
DEFAULT_NEWS_PROGRESS_CSV = (
    "results/news_reasoning/final_environmental_causal_dataset_2014_plus_news_progress.csv"
)
DEFAULT_SUMMARY_CSV = (
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5_validated.csv"
)
DEFAULT_CAUSAL_CSV = (
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus_type_normalized.csv"
)
DEFAULT_SATELLITE_SUMMARY_CSV = (
    "results/satellite/batch_summary_massive_2014_plus_flood.csv"
)
DEFAULT_SATELLITE_RANKING_CSV = (
    "results/satellite/ranked_events_massive_2014_plus_flood.csv"
)
DEFAULT_OUTPUT_CSV = "results/final_environmental_causal_dataset_2014_plus.csv"
DEFAULT_OUTPUT_SUMMARY_CSV = "results/final_environmental_causal_dataset_2014_plus_summary.csv"
EVENT_ID_COLUMNS = ("event_id", "disaster_id", "emdat_disaster_id", "DisNo.")
RAW_OR_HEAVY_COLUMNS = {
    "llm_prompt",
    "summary_prompt",
    "summary_raw_response",
    "causal_chain_prompt",
    "causal_chain_raw_response",
    "raw_result_json",
}


# Cleans scalar values used as identifiers or compact CSV values.
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return " ".join(text.replace("\n", " ").replace("\r", " ").split())


# Returns the first column available in a DataFrame.
def first_column(columns: Iterable[str], names: Iterable[str]) -> Optional[str]:
    available = set(columns)
    for name in names:
        if name in available:
            return name
    return None


# Reads a CSV if present, otherwise returns an empty frame with a warning.
def read_csv(path: Path, *, label: str, required: bool = False) -> pd.DataFrame:
    if not path.exists():
        if required:
            raise SystemExit(f"Required {label} not found: {path}")
        print(f"Warning: optional {label} not found, skipping it: {path}")
        return pd.DataFrame()
    return pd.read_csv(path, dtype=object, low_memory=False)


# Standardizes the event id column and keeps the latest duplicate row.
def standardize_event_id(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if frame.empty:
        return frame

    id_column = first_column(frame.columns, EVENT_ID_COLUMNS)
    if id_column is None:
        print(f"Warning: {label} has no event id column, skipping it.")
        return pd.DataFrame()

    output = frame.copy()
    if id_column == "event_id":
        output["event_id"] = output["event_id"].map(clean_text)
    else:
        output.insert(0, "event_id", output[id_column].map(clean_text))
        output = output.drop(columns=[id_column])
    output = output[output["event_id"].ne("")].copy()

    duplicate_count = int(output["event_id"].duplicated().sum())
    if duplicate_count:
        print(f"Warning: {label} has {duplicate_count} duplicated event id rows; keeping the latest.")
        output = output.drop_duplicates("event_id", keep="last")

    return output


# Builds a normalized key used only to avoid duplicated source columns.
def comparable_column_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


# Renames selected columns and drops source fields that would only duplicate noise.
def compact_source_frame(
    frame: pd.DataFrame,
    *,
    label: str,
    rename_map: Mapping[str, str],
    keep_columns: Sequence[str],
) -> pd.DataFrame:
    frame = standardize_event_id(frame, label=label)
    if frame.empty:
        return frame

    columns = ["event_id"] + [column for column in keep_columns if column in frame.columns]
    output = frame[columns].copy()
    rename = {old: new for old, new in rename_map.items() if old in output.columns}
    output = output.rename(columns=rename)
    return output


# Keeps useful GDIS/base columns without repeating fields already in geocoding.
def compact_base_frame(base: pd.DataFrame, master_columns: Iterable[str]) -> pd.DataFrame:
    base = standardize_event_id(base, label="base dataset")
    if base.empty:
        return base

    existing = {comparable_column_name(column) for column in master_columns}
    keep = []
    for column in base.columns:
        if column == "event_id" or column in RAW_OR_HEAVY_COLUMNS:
            continue
        if comparable_column_name(column) in existing:
            continue
        keep.append(column)

    output = base[["event_id"] + keep].copy()
    return output.rename(columns={column: f"base_{column}" for column in keep})


# Joins a source table to the master dataset by event_id.
def left_join(master: pd.DataFrame, source: pd.DataFrame) -> pd.DataFrame:
    if source.empty:
        return master
    overlap = [column for column in source.columns if column != "event_id" and column in master.columns]
    if overlap:
        source = source.drop(columns=overlap)
    return master.merge(source, on="event_id", how="left")


# Converts numeric-looking columns after joins without touching text columns.
def numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series([pd.NA] * len(frame), index=frame.index, dtype="object")
    return pd.to_numeric(frame[column], errors="coerce")


# Calculates one percentage row for the final summary.
def metric_row(metric: str, count: int, total: int) -> Dict[str, Any]:
    percent = 100.0 if metric == "total_events" and total else (count / total * 100 if total else 0)
    return {
        "metric": metric,
        "count": int(count),
        "percent_of_total_events": round(percent, 2),
    }


# Builds a compact coverage report for the master dataset.
def build_summary(master: pd.DataFrame) -> pd.DataFrame:
    total = len(master)
    latitude = numeric_series(master, "latitude")
    longitude = numeric_series(master, "longitude")
    coordinates = latitude.notna() & longitude.notna()
    articles = numeric_series(master, "news_total_articles_retrieved")
    chain_length = numeric_series(master, "causal_chain_length").fillna(0)

    weather_status = master.get("weather_retrieval_status", pd.Series([""] * total, index=master.index)).fillna("")
    status = master.get("summary_validation_status", pd.Series([""] * total)).fillna("")
    satellite_status = master.get("satellite_status", pd.Series([""] * total)).fillna("")

    rows = [
        metric_row("total_events", total, total),
        metric_row("events_with_coordinates", int(coordinates.sum()), total),
        metric_row("events_without_coordinates", int((~coordinates).sum()), total),
        metric_row("weather_records_fetched", int(weather_status.eq("fetched").sum()), total),
        metric_row("events_with_news_articles", int((articles.fillna(0) > 0).sum()), total),
        metric_row("accepted_event_summaries", int(status.eq("accepted").sum()), total),
        metric_row("events_with_non_empty_causal_chain", int((chain_length > 0).sum()), total),
        metric_row("events_with_satellite_record", int(satellite_status.ne("").sum()), total),
        metric_row(
            "satellite_completed_or_existing",
            int(satellite_status.isin(["completed", "skipped_existing"]).sum()),
            total,
        ),
    ]
    return pd.DataFrame(rows)


# Moves the most important columns to the beginning of the final CSV.
def order_columns(frame: pd.DataFrame) -> pd.DataFrame:
    preferred = [
        "event_id",
        "ISO",
        "Country",
        "Subregion",
        "Region",
        "Location",
        "_llm_start_date",
        "Disaster Group",
        "Disaster Subgroup",
        "Disaster Type",
        "Disaster Subtype",
        "Event Name",
        "latitude",
        "longitude",
        "position_source",
        "weather_retrieval_status",
        "pre_total_rainfall_mm",
        "pre_max_daily_rainfall_mm",
        "post_total_rainfall_mm",
        "post_max_daily_rainfall_mm",
        "news_retrieval_status",
        "news_total_articles_retrieved",
        "summary_validation_status",
        "event_summary",
        "causal_chain_length",
        "causal_chain_parse_status",
        "causal_chain_json",
        "satellite_status",
        "satellite_rank_group",
        "satellite_rank_score",
        "satellite_manifest_path",
    ]
    first = [column for column in preferred if column in frame.columns]
    rest = [column for column in frame.columns if column not in first]
    return frame[first + rest]


# Builds the single event-level dataset from all final pipeline artifacts.
def assemble_final_dataset(args: argparse.Namespace) -> Tuple[pd.DataFrame, pd.DataFrame]:
    geocoding = read_csv(Path(args.geocoding_csv), label="final geocoding CSV", required=True)
    master = standardize_event_id(geocoding, label="final geocoding CSV")
    master = master.drop(columns=[column for column in RAW_OR_HEAVY_COLUMNS if column in master.columns])

    base = read_csv(Path(args.base_csv), label="base EM-DAT/GDIS CSV")
    master = left_join(master, compact_base_frame(base, master.columns))

    weather = read_csv(Path(args.weather_progress_csv), label="weather progress CSV")
    weather = compact_source_frame(
        weather,
        label="weather progress CSV",
        keep_columns=[
            "weather_retrieval_status",
            "pre_total_rainfall_mm",
            "pre_max_daily_rainfall_mm",
            "pre_avg_max_temperature_c",
            "post_total_rainfall_mm",
            "post_max_daily_rainfall_mm",
            "post_avg_max_temperature_c",
            "weather_batch_version",
        ],
        rename_map={},
    )
    master = left_join(master, weather)

    news = read_csv(Path(args.news_progress_csv), label="news progress CSV")
    news = compact_source_frame(
        news,
        label="news progress CSV",
        keep_columns=[
            "news_data_searched",
            "news_retrieval_status",
            "total_articles_retrieved",
            "sources_successfully_resolved",
            "news_engine_version",
            "source_mode",
        ],
        rename_map={
            "total_articles_retrieved": "news_total_articles_retrieved",
            "sources_successfully_resolved": "news_sources_successfully_resolved",
        },
    )
    master = left_join(master, news)

    summary = read_csv(Path(args.summary_csv), label="validated summary CSV")
    summary = compact_source_frame(
        summary,
        label="validated summary CSV",
        keep_columns=[
            "model_name",
            "llm_call_status",
            "event_summary",
            "summary_validation_status",
            "summary_prompt_version",
            "summary_relevant_news_count",
            "news_rejected_for_summary",
            "selected_news_count",
            "usable_news_count",
            "news_input_quality",
            "news_total_chars",
        ],
        rename_map={
            "model_name": "summary_model_name",
            "llm_call_status": "summary_llm_call_status",
            "selected_news_count": "summary_selected_news_count",
            "usable_news_count": "summary_usable_news_count",
            "news_input_quality": "summary_news_input_quality",
            "news_total_chars": "summary_news_total_chars",
        },
    )
    master = left_join(master, summary)

    causal = read_csv(Path(args.causal_csv), label="normalized causal-chain CSV")
    causal = compact_source_frame(
        causal,
        label="normalized causal-chain CSV",
        keep_columns=[
            "model_name",
            "llm_call_status",
            "causal_chain_json",
            "causal_chain_length",
            "causal_chain_parse_status",
            "causal_chain_prompt_version",
            "causal_chain_dropped_quote_steps",
            "causal_chain_fuzzy_quote_steps",
            "causal_chain_type_events_changed",
            "causal_chain_type_normalizer_version",
        ],
        rename_map={
            "model_name": "causal_model_name",
            "llm_call_status": "causal_llm_call_status",
        },
    )
    master = left_join(master, causal)

    satellite = read_csv(Path(args.satellite_summary_csv), label="satellite summary CSV")
    satellite = compact_source_frame(
        satellite,
        label="satellite summary CSV",
        keep_columns=[
            "run_started_at",
            "status",
            "manifest_path",
            "recommended_primary_layer",
            "s1_change_detection_available",
            "s2_pre_usable",
            "s2_post_usable",
            "s2_change_detection_usable",
            "s2_water_change_mask_available",
            "s2_pre_local_cloud_cover",
            "s2_post_local_cloud_cover",
            "s1_candidate_area_km2",
            "s2_pre_candidate_water_area_km2",
            "s2_post_candidate_water_area_km2",
            "s2_candidate_new_water_area_km2",
            "output_count",
            "elapsed_seconds",
            "error",
        ],
        rename_map={
            "run_started_at": "satellite_run_started_at",
            "status": "satellite_status",
            "manifest_path": "satellite_manifest_path",
            "recommended_primary_layer": "satellite_recommended_primary_layer",
            "s1_change_detection_available": "satellite_s1_change_detection_available",
            "s2_pre_usable": "satellite_s2_pre_usable",
            "s2_post_usable": "satellite_s2_post_usable",
            "s2_change_detection_usable": "satellite_s2_change_detection_usable",
            "s2_water_change_mask_available": "satellite_s2_water_change_mask_available",
            "s2_pre_local_cloud_cover": "satellite_s2_pre_local_cloud_cover",
            "s2_post_local_cloud_cover": "satellite_s2_post_local_cloud_cover",
            "s1_candidate_area_km2": "satellite_s1_candidate_area_km2",
            "s2_pre_candidate_water_area_km2": "satellite_s2_pre_candidate_water_area_km2",
            "s2_post_candidate_water_area_km2": "satellite_s2_post_candidate_water_area_km2",
            "s2_candidate_new_water_area_km2": "satellite_s2_candidate_new_water_area_km2",
            "output_count": "satellite_output_count",
            "elapsed_seconds": "satellite_elapsed_seconds",
            "error": "satellite_error",
        },
    )
    master = left_join(master, satellite)

    ranking = read_csv(Path(args.satellite_ranking_csv), label="satellite ranking CSV")
    ranking = compact_source_frame(
        ranking,
        label="satellite ranking CSV",
        keep_columns=["rank", "rank_group", "rank_score", "rank_reason"],
        rename_map={
            "rank": "satellite_rank",
            "rank_group": "satellite_rank_group",
            "rank_score": "satellite_rank_score",
            "rank_reason": "satellite_rank_reason",
        },
    )
    master = left_join(master, ranking)

    if "causal_chain_json" in master.columns:
        master["causal_chain_json"] = master["causal_chain_json"].map(
            lambda value: clean_text(value) or json.dumps({"causal_chain": []})
        )

    master = order_columns(master)
    return master, build_summary(master)


# Defines the command-line options for final dataset assembly.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assemble a single event-level CSV from all final pipeline outputs."
    )
    parser.add_argument("--geocoding-csv", default=DEFAULT_GEOCODING_CSV)
    parser.add_argument("--base-csv", default=DEFAULT_BASE_CSV)
    parser.add_argument("--weather-progress-csv", default=DEFAULT_WEATHER_PROGRESS_CSV)
    parser.add_argument("--news-progress-csv", default=DEFAULT_NEWS_PROGRESS_CSV)
    parser.add_argument("--summary-csv", default=DEFAULT_SUMMARY_CSV)
    parser.add_argument("--causal-csv", default=DEFAULT_CAUSAL_CSV)
    parser.add_argument("--satellite-summary-csv", default=DEFAULT_SATELLITE_SUMMARY_CSV)
    parser.add_argument("--satellite-ranking-csv", default=DEFAULT_SATELLITE_RANKING_CSV)
    parser.add_argument("--output-csv", default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--output-summary-csv", default=DEFAULT_OUTPUT_SUMMARY_CSV)
    return parser.parse_args()


# Writes the master CSV and prints the final file overview.
def main() -> None:
    args = parse_args()
    output_csv = Path(args.output_csv)
    output_summary_csv = Path(args.output_summary_csv)

    master, summary = assemble_final_dataset(args)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    master.to_csv(output_csv, index=False)
    output_summary_csv.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output_summary_csv, index=False)

    print("Final complete dataset:", output_csv)
    print("Final complete dataset summary:", output_summary_csv)
    print("Rows:", len(master))
    print("Columns:", len(master.columns))
    print()
    print("Final coverage snapshot:")
    print(summary.to_string(index=False))
    print()
    print("Separate detail files remain available:")
    print("Final geocoding dataset:", args.geocoding_csv)
    print("Weather progress CSV:", args.weather_progress_csv)
    print("News progress CSV:", args.news_progress_csv)
    print("Validated summaries CSV:", args.summary_csv)
    print("Normalized causal chains CSV:", args.causal_csv)
    print("Satellite flood summary CSV:", args.satellite_summary_csv)
    print("Satellite ranking CSV:", args.satellite_ranking_csv)


if __name__ == "__main__":
    main()
