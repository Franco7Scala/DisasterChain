from __future__ import annotations

import argparse
from pathlib import Path

from support.constants import (
    CLEANED_DISASTERS_OUTPUT_PATH,
    EMDAT_INPUT_PATH,
    GDIS_INPUT_PATH,
)
from support.utils import merge_and_clean_datasets


# Defines the command-line options for building the merged base dataset.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge EM-DAT and GDIS into the base event table."
    )
    parser.add_argument("--emdat-file", default=EMDAT_INPUT_PATH)
    parser.add_argument("--gdis-file", default=GDIS_INPUT_PATH)
    parser.add_argument("--output-csv", default=CLEANED_DISASTERS_OUTPUT_PATH)
    return parser.parse_args()


# Reads the raw datasets and writes the merged base CSV used by later steps.
def main() -> None:
    args = parse_args()
    emdat_file = Path(args.emdat_file)
    gdis_file = Path(args.gdis_file)
    output_csv = Path(args.output_csv)

    if not emdat_file.exists():
        raise SystemExit(f"EM-DAT input file not found: {emdat_file}")
    if not gdis_file.exists():
        raise SystemExit(f"GDIS input file not found: {gdis_file}")

    merged = merge_and_clean_datasets(str(emdat_file), str(gdis_file))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_csv, index=False)

    print("EM-DAT input:", emdat_file)
    print("GDIS input:", gdis_file)
    print("Merged rows:", len(merged))
    print("Output CSV:", output_csv)


if __name__ == "__main__":
    main()
