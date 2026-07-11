import argparse
from pathlib import Path
from typing import List

import pandas as pd

from support.constants import EMDAT_INPUT_PATH, RECENT_EMDAT_GEOCODING_OUTPUT_DIR
from support.location_reasoning import (
    build_location_prompt_from_row,
    normalize_canonical_location_response,
)
from support.reasoner import DEFAULT_REASONER_MODEL, Reasoner


REQUIRED_COLUMNS = {"Country", "Subregion", "Region", "Location"}


def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path)


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


def selected_rows(frame: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise SystemExit("Input missing column(s): " + ", ".join(sorted(missing)))

    rows = frame.copy()
    if args.start_date:
        start_dates = build_start_date(rows)
        rows = rows[start_dates.ge(pd.Timestamp(args.start_date))].copy()
        rows["_llm_start_date"] = start_dates.loc[rows.index].dt.strftime("%Y-%m-%d")

    if args.offset:
        rows = rows.iloc[args.offset :].copy()
    if args.limit:
        rows = rows.head(args.limit).copy()
    return rows


def output_columns(frame: pd.DataFrame) -> List[str]:
    preferred = [
        "DisNo.",
        "Country",
        "Subregion",
        "Region",
        "Location",
        "_llm_start_date",
    ]
    return [column for column in preferred if column in frame.columns]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use the shared Reasoner LLM wrapper to normalize EM-DAT location "
            "text before Nominatim geocoding."
        )
    )
    parser.add_argument("--input-file", default=EMDAT_INPUT_PATH)
    parser.add_argument(
        "--output-csv",
        default=str(
            Path(RECENT_EMDAT_GEOCODING_OUTPUT_DIR)
            / "llm_location_cleaning_test_from_2014-04-03.csv"
        ),
    )
    parser.add_argument("--model-name", default=DEFAULT_REASONER_MODEL)
    parser.add_argument("--start-date", default="2014-04-03")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--load-in-4bit",
        action="store_true",
        help="Load the HuggingFace model with 4-bit quantization.",
    )
    parser.add_argument(
        "--load-in-8bit",
        action="store_true",
        help="Load the HuggingFace model with 8-bit quantization.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only write prompts; do not load or call the LLM.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=64)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    frame = read_table(Path(args.input_file))
    rows = selected_rows(frame, args)

    reasoner = None
    if not args.dry_run:
        reasoner = Reasoner(
            args.model_name,
            load_in_4bit=args.load_in_4bit,
            load_in_8bit=args.load_in_8bit,
            max_new_tokens=args.max_new_tokens,
            do_sample=False,
        )

    output_rows = []
    total_rows = len(rows)
    for position, (index, row) in enumerate(rows.iterrows(), start=1):
        prompt = build_location_prompt_from_row(row)
        raw_response = ""
        canonical = ""
        if args.dry_run:
            canonical = ""
        else:
            dis_no = row.get("DisNo.", index)
            location = row.get("Location", "")
            print(f"[{position}/{total_rows}] {dis_no}: {location}", flush=True)
            raw_response = reasoner.ask(prompt)
            canonical = normalize_canonical_location_response(raw_response)

        result = {column: row.get(column, "") for column in output_columns(rows)}
        result.update(
            {
                "row_index": index,
                "model_name": args.model_name,
                "llm_prompt": prompt,
                "llm_raw_response": raw_response,
                "llm_canonical_location": canonical,
            }
        )
        output_rows.append(result)

    output = pd.DataFrame(output_rows)
    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_path, index=False)

    print(f"Input rows selected: {len(rows)}")
    print(f"Dry run: {args.dry_run}")
    print(f"Output CSV: {output_path}")
    if args.dry_run and len(output):
        print()
        print("First prompt:")
        print(output.iloc[0]["llm_prompt"])


if __name__ == "__main__":
    main()
