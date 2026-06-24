import argparse
import time
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

import pandas as pd
import requests

from support.constants import (
    GADM_DATA_DIR,
    GADM_DOWNLOAD_PLAN_CSV,
    GADM_FILE_PREFIX,
    GADM_GEOJSON_BASE_URL,
    HEADERS,
    RECENT_EMDAT_ADMIN_UNITS_CSV,
)


RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def parse_csv_set(values: Optional[str]) -> Set[str]:
    if not values:
        return set()
    return {value.strip().upper() for value in values.split(",") if value.strip()}


def normalize_level(value) -> Optional[int]:
    try:
        level = int(float(value))
    except (TypeError, ValueError):
        return None
    return level if 0 <= level <= 5 else None


def gadm_filename(iso: str, level: int) -> str:
    return f"{GADM_FILE_PREFIX}_{iso}_{level}.json.zip"


def gadm_url(iso: str, level: int) -> str:
    return f"{GADM_GEOJSON_BASE_URL}/{gadm_filename(iso, level)}"


def find_existing_file(gadm_dir: Path, iso: str, level: int) -> Optional[Path]:
    stems = [
        f"{GADM_FILE_PREFIX}_{iso}_{level}.json.zip",
        f"{GADM_FILE_PREFIX}_{iso}_{level}.json",
        f"{GADM_FILE_PREFIX}_{iso}_{level}.geojson.zip",
        f"{GADM_FILE_PREFIX}_{iso}_{level}.geojson",
    ]
    for name in stems:
        path = gadm_dir / name
        if path.exists():
            return path
    return None


def build_download_plan(admin_units: pd.DataFrame, gadm_dir: Path) -> pd.DataFrame:
    rows = []
    usable = admin_units.copy()
    usable["unit_level_norm"] = usable["unit_level"].apply(normalize_level)
    usable = usable[
        usable["iso"].notna()
        & usable["country"].notna()
        & usable["unit_level_norm"].notna()
    ].copy()
    usable["iso"] = usable["iso"].astype(str).str.upper()
    usable["unit_level_norm"] = usable["unit_level_norm"].astype(int)

    country_events = (
        usable.groupby(["iso", "country"])["emdat_disaster_id"]
        .nunique()
        .rename("country_candidate_events")
        .reset_index()
    )

    for (iso, country, level), group in usable.groupby(["iso", "country", "unit_level_norm"]):
        gadm_id_group = group[group["source_field"].eq("GADM Admin Units")]
        existing_path = find_existing_file(gadm_dir, iso, int(level))
        rows.append(
            {
                "iso": iso,
                "country": country,
                "unit_level": int(level),
                "candidate_events": group["emdat_disaster_id"].nunique(),
                "gadm_id_events": gadm_id_group["emdat_disaster_id"].nunique(),
                "admin_unit_rows": len(group),
                "existing_file": str(existing_path) if existing_path else "",
                "needs_download": existing_path is None,
                "url": gadm_url(iso, int(level)),
                "target_file": str(gadm_dir / gadm_filename(iso, int(level))),
            }
        )

    plan = pd.DataFrame(rows)
    if plan.empty:
        return plan
    plan = plan.merge(country_events, on=["iso", "country"], how="left")
    return plan.sort_values(
        [
            "country_candidate_events",
            "gadm_id_events",
            "candidate_events",
            "iso",
            "unit_level",
        ],
        ascending=[False, False, False, True, True],
    ).reset_index(drop=True)


def filter_plan(
    plan: pd.DataFrame,
    countries: Set[str],
    exclude_countries: Set[str],
    unit_level: Optional[int],
    top_countries: Optional[int],
    top_items: Optional[int],
) -> pd.DataFrame:
    if plan.empty:
        return plan
    filtered = plan.copy()
    if countries:
        filtered = filtered[filtered["iso"].isin(countries)]
    if exclude_countries:
        filtered = filtered[~filtered["iso"].isin(exclude_countries)]
    if unit_level is not None:
        filtered = filtered[filtered["unit_level"].eq(unit_level)]
    if top_countries is not None and top_countries > 0 and not countries:
        top_isos = (
            filtered.drop_duplicates("iso")
            .sort_values("country_candidate_events", ascending=False)
            .head(top_countries)["iso"]
        )
        filtered = filtered[filtered["iso"].isin(set(top_isos))]
    if top_items is not None and top_items > 0:
        filtered = filtered.head(top_items)
    return filtered.reset_index(drop=True)


def download_file(
    url: str,
    target_path: Path,
    timeout_seconds: int,
    retries: int,
) -> Tuple[bool, str]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = target_path.with_suffix(target_path.suffix + ".part")

    for attempt in range(1, retries + 2):
        try:
            response = requests.get(
                url,
                headers=HEADERS,
                stream=True,
                timeout=timeout_seconds,
            )
            if response.status_code == 404:
                return False, "not_found"
            if response.status_code in RETRY_STATUS_CODES and attempt <= retries:
                time.sleep(2 * attempt)
                continue
            response.raise_for_status()

            with partial_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        handle.write(chunk)
            partial_path.replace(target_path)
            return True, "downloaded"
        except requests.RequestException as exc:
            if attempt > retries:
                return False, f"failed: {exc}"
            time.sleep(2 * attempt)

    return False, "failed"


def print_plan(plan: pd.DataFrame) -> None:
    if plan.empty:
        print("No GADM downloads selected.")
        return
    columns = [
        "iso",
        "country",
        "unit_level",
        "candidate_events",
        "gadm_id_events",
        "needs_download",
        "target_file",
    ]
    print(plan[columns].to_string(index=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prioritize and optionally download GADM GeoJSON files needed "
            "to build administrative-unit bounding boxes."
        )
    )
    parser.add_argument("--admin-units-csv", default=RECENT_EMDAT_ADMIN_UNITS_CSV)
    parser.add_argument("--gadm-dir", default=GADM_DATA_DIR)
    parser.add_argument("--plan-csv", default=GADM_DOWNLOAD_PLAN_CSV)
    parser.add_argument(
        "--country",
        help="Comma-separated ISO3 country codes to include, e.g. IDN,IND,PHL.",
    )
    parser.add_argument(
        "--exclude-country",
        help="Comma-separated ISO3 country codes to exclude, e.g. USA,CHN.",
    )
    parser.add_argument(
        "--unit-level",
        type=int,
        choices=range(0, 6),
        help="Download only the selected GADM administrative level, e.g. 2.",
    )
    parser.add_argument("--top-countries", type=int, default=10)
    parser.add_argument("--top-items", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    parser.add_argument(
        "--download",
        action="store_true",
        help="Actually download missing files. Without this flag, only the plan is written.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for plan-only mode; no files are downloaded.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    admin_units_path = Path(args.admin_units_csv)
    gadm_dir = Path(args.gadm_dir)
    plan_path = Path(args.plan_csv)

    if not admin_units_path.exists():
        raise SystemExit(
            "Admin units CSV not found.\n"
            f"Expected path: {admin_units_path}\n\n"
            "Run analyze_recent_emdat_geocoding.py first."
        )

    admin_units = pd.read_csv(admin_units_path)
    plan = build_download_plan(admin_units, gadm_dir)
    selected = filter_plan(
        plan,
        countries=parse_csv_set(args.country),
        exclude_countries=parse_csv_set(args.exclude_country),
        unit_level=args.unit_level,
        top_countries=args.top_countries,
        top_items=args.top_items,
    )

    plan_path.parent.mkdir(parents=True, exist_ok=True)
    selected.to_csv(plan_path, index=False)

    print(f"Download plan rows: {len(selected)}")
    print(f"Plan CSV: {plan_path}")
    print_plan(selected.head(30))

    should_download = args.download and not args.dry_run
    if not should_download:
        print("\nPlan-only mode. Add --download to fetch missing files.")
        return

    downloaded = 0
    skipped_existing = 0
    failed = 0
    for _, row in selected.iterrows():
        target_path = Path(row["target_file"])
        existing_path = find_existing_file(gadm_dir, row["iso"], int(row["unit_level"]))
        if existing_path:
            skipped_existing += 1
            print(f"Skipping existing: {existing_path}")
            continue

        print(f"Downloading {row['iso']} level {row['unit_level']}: {row['url']}")
        ok, status = download_file(
            row["url"],
            target_path,
            timeout_seconds=args.timeout_seconds,
            retries=args.retries,
        )
        if ok:
            downloaded += 1
            print(f"Saved: {target_path}")
        else:
            failed += 1
            print(f"Download failed for {row['iso']} level {row['unit_level']}: {status}")
        time.sleep(args.sleep_seconds)

    print("\nDownload summary:")
    print(f"downloaded: {downloaded}")
    print(f"skipped_existing: {skipped_existing}")
    print(f"failed: {failed}")


if __name__ == "__main__":
    main()
