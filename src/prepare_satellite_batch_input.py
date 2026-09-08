from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import pandas as pd


DEFAULT_INPUT_CSV = Path(
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_OUTPUT_CSV = Path("results/multimodal_satellite_2014_plus/events.csv")


# Returns the first column available in the input file.
def first_column(columns: Iterable[str], names: Iterable[str]) -> Optional[str]:
    available = set(columns)
    for name in names:
        if name in available:
            return name
    return None


# Returns a cleaned string value from one row.
def clean_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


# Builds a start date from either an existing date column or EM-DAT date parts.
def start_dates_from_frame(frame: pd.DataFrame) -> pd.Series:
    direct_column = first_column(frame.columns, ("start_date", "Start Date", "_llm_start_date"))
    if direct_column:
        return pd.to_datetime(frame[direct_column], errors="coerce")

    year_column = first_column(frame.columns, ("Start Year", "start_year"))
    month_column = first_column(frame.columns, ("Start Month", "start_month"))
    day_column = first_column(frame.columns, ("Start Day", "start_day"))
    if not all([year_column, month_column, day_column]):
        raise SystemExit("Missing start date columns")

    return pd.to_datetime(
        {
            "year": pd.to_numeric(frame[year_column], errors="coerce"),
            "month": pd.to_numeric(frame[month_column], errors="coerce"),
            "day": pd.to_numeric(frame[day_column], errors="coerce"),
        },
        errors="coerce",
    )


# Finds all columns needed by the satellite batch runner.
def required_columns(frame: pd.DataFrame) -> Mapping[str, Optional[str]]:
    return {
        "event_id": first_column(frame.columns, ("emdat_disaster_id", "DisNo.", "disaster_id", "event_id")),
        "country": first_column(frame.columns, ("country", "Country")),
        "location": first_column(frame.columns, ("location", "Location", "emdat_location")),
        "disaster_type": first_column(frame.columns, ("disaster_type", "Disaster Type")),
        "latitude": first_column(frame.columns, ("latitude", "Latitude", "lat", "final_latitude")),
        "longitude": first_column(frame.columns, ("longitude", "Longitude", "lon", "final_longitude")),
        "position_source": first_column(frame.columns, ("position_source",)),
    }


# Converts event fields and records why a row cannot enter the satellite batch.
def satellite_input_with_audit(
    frame: pd.DataFrame,
    *,
    disaster_type: str,
    min_start_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = required_columns(frame)
    required = ("event_id", "country", "disaster_type", "latitude", "longitude")
    missing = [name for name in required if columns[name] is None]
    if missing:
        raise SystemExit("Missing required column(s): " + ", ".join(missing))

    dates = start_dates_from_frame(frame)
    output = pd.DataFrame(
        {
            "emdat_disaster_id": frame[columns["event_id"]].map(clean_value),
            "country": frame[columns["country"]].map(clean_value),
            "location": (
                frame[columns["location"]].map(clean_value)
                if columns["location"]
                else ""
            ),
            "start_date": dates.dt.strftime("%Y-%m-%d"),
            "disaster_type": frame[columns["disaster_type"]].map(clean_value),
            "latitude": pd.to_numeric(frame[columns["latitude"]], errors="coerce"),
            "longitude": pd.to_numeric(frame[columns["longitude"]], errors="coerce"),
        }
    )
    if columns["position_source"]:
        output["position_source"] = frame[columns["position_source"]].map(clean_value)

    ids = output["emdat_disaster_id"]
    if ids[ids.ne("")].duplicated().any():
        raise SystemExit("Duplicate event ids in input; resolve them before starting the batch")
    reason = pd.Series("selected", index=output.index)
    checks = [
        (ids.eq("") | ids.str.contains(r"[/\\\\:]") | ids.isin([".", ".."]), "invalid_event_id"),
        (output["disaster_type"].eq(""), "missing_disaster_type"),
        (dates.isna(), "missing_or_invalid_date"),
        (output[["latitude", "longitude"]].isna().any(axis=1), "missing_coordinates"),
        (~output["latitude"].between(-90, 90, inclusive="neither")
         | ~output["longitude"].between(-180, 180), "invalid_coordinates"),
    ]
    if disaster_type:
        checks.append((~output["disaster_type"].str.casefold().eq(disaster_type.casefold()), "type_filter"))
    if min_start_date:
        checks.append((dates.lt(pd.Timestamp(min_start_date)), "before_start_date"))
    for mask, label in checks:
        reason.loc[reason.eq("selected") & mask] = label
    audit = output[["emdat_disaster_id", "disaster_type", "start_date"]].copy()
    audit["selection_status"] = reason
    return output.loc[reason.eq("selected")].reset_index(drop=True), audit


# Returns only rows with usable dates and coordinates, without restricting the disaster type.
def build_satellite_input(frame: pd.DataFrame, *, disaster_type: str = "", min_start_date: str = "2014-04-03") -> pd.DataFrame:
    return satellite_input_with_audit(frame, disaster_type=disaster_type, min_start_date=min_start_date)[0]


# Defines the command-line options for preparing satellite batch input.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a compact event CSV for the satellite batch runner."
    )
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--disaster-type", default="", help="Optional type filter; defaults to all types")
    parser.add_argument("--min-start-date", default="2014-04-03")
    return parser.parse_args()


# Reads the final geocoding CSV and writes the satellite-ready event table.
def main() -> None:
    args = parse_args()
    input_csv = Path(args.input_csv)
    output_csv = Path(args.output_csv)
    if not input_csv.exists():
        raise SystemExit(f"Input CSV not found: {input_csv}")

    frame = pd.read_csv(input_csv, low_memory=False)
    output, audit = satellite_input_with_audit(
        frame,
        disaster_type=args.disaster_type,
        min_start_date=args.min_start_date,
    )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_csv, index=False)
    audit_csv = output_csv.with_name(output_csv.stem + "_selection_audit.csv")
    summary_csv = output_csv.with_name(output_csv.stem + "_selection_summary.csv")
    audit.to_csv(audit_csv, index=False)
    selection_summary = audit.groupby(["disaster_type", "selection_status"], dropna=False).size().reset_index(name="count")
    selection_summary.to_csv(summary_csv, index=False)

    print("Input rows:", len(frame))
    print("Satellite rows:", len(output))
    print("Disaster type:", args.disaster_type or "all")
    print("Min start date:", args.min_start_date or "none")
    print("Output CSV:", output_csv)
    print("Selection audit CSV:", audit_csv)
    print("Selection summary CSV:", summary_csv)
    print(selection_summary.to_string(index=False))
    if len(output):
        print()
        print(output.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
