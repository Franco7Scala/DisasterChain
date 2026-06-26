import argparse
from difflib import SequenceMatcher
import json
import math
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from support.constants import (
    GADM_DATA_DIR,
    RECENT_EMDAT_ADMIN_UNIT_BBOXES_CSV,
    RECENT_EMDAT_ADMIN_UNITS_CSV,
)


GID_RE = re.compile(r"^[A-Z]{3}\.")
GID_VERSION_RE = re.compile(r"_\d+$")
NAME_SEPARATOR_RE = re.compile(r"[/|;]+")
NON_WORD_RE = re.compile(r"[^\w\s-]+", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
DEFAULT_FUZZY_NAME_THRESHOLD = 0.92


def normalize_text(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value).strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    text = text.replace("-", " ")
    text = NON_WORD_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_unit_id(value) -> str:
    return str(value).strip() if value is not None and str(value).strip() != "nan" else ""


def normalize_gadm_id_base(value: str) -> str:
    return GID_VERSION_RE.sub("", normalize_unit_id(value))


def name_variants(value) -> List[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    raw_parts = [str(value)]
    raw_parts.extend(NAME_SEPARATOR_RE.split(str(value)))

    variants = []
    for part in raw_parts:
        normalized = normalize_text(part)
        if normalized and normalized not in variants:
            variants.append(normalized)
    return variants


def add_unique_index(index: Dict[Tuple, Optional[Dict]], key: Tuple, record: Dict) -> None:
    current = index.get(key)
    if current is None and key in index:
        return
    if current is not None and current.get("unit_id") != record.get("unit_id"):
        index[key] = None
        return
    index[key] = record


def is_geojson_path(path: Path) -> bool:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    return (
        path.suffix.lower() in {".json", ".geojson"}
        or path.suffix.lower() == ".zip"
        or suffixes[-2:] == [".json", ".zip"]
        or suffixes[-2:] == [".geojson", ".zip"]
    )


def find_geojson_files(gadm_dir: Path) -> List[Path]:
    if not gadm_dir.exists():
        return []
    return sorted(path for path in gadm_dir.rglob("*") if path.is_file() and is_geojson_path(path))


def load_geojson(path: Path) -> Dict:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".json", ".geojson"))
            ]
            if not names:
                raise ValueError(f"No JSON/GeoJSON file found inside {path}")
            documents = []
            for name in names:
                with archive.open(name) as handle:
                    documents.append(json.load(handle))
            if len(documents) == 1:
                return documents[0]

            features = []
            for document in documents:
                if document.get("type") == "FeatureCollection":
                    features.extend(document.get("features", []))
                elif document.get("type") == "Feature":
                    features.append(document)
            return {"type": "FeatureCollection", "features": features}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def iter_positions(coordinates) -> Iterable[Tuple[float, float]]:
    if not isinstance(coordinates, list):
        return
    if len(coordinates) >= 2 and all(isinstance(value, (int, float)) for value in coordinates[:2]):
        yield float(coordinates[0]), float(coordinates[1])
        return
    for item in coordinates:
        yield from iter_positions(item)


def geometry_bbox(geometry: Dict) -> Optional[Tuple[float, float, float, float]]:
    if not geometry:
        return None
    positions = list(iter_positions(geometry.get("coordinates")))
    if not positions:
        return None
    xs = [position[0] for position in positions]
    ys = [position[1] for position in positions]
    return min(xs), min(ys), max(xs), max(ys)


def property_value(properties: Dict, *names: str) -> str:
    for name in names:
        for key in (name, name.upper(), name.lower()):
            value = properties.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def property_values(properties: Dict, *names: str) -> List[str]:
    values = []
    for name in names:
        for key in (name, name.upper(), name.lower()):
            value = properties.get(key)
            if value is None or not str(value).strip():
                continue
            for part in str(value).split("|"):
                text = part.strip()
                if text and text not in values:
                    values.append(text)
    return values


def feature_level(properties: Dict) -> Optional[int]:
    for level in (5, 4, 3, 2, 1, 0):
        if property_value(properties, f"GID_{level}") or property_value(properties, f"NAME_{level}"):
            return level
    return None


def feature_record(feature: Dict, source_file: Path) -> Optional[Dict]:
    properties = feature.get("properties") or {}
    level = feature_level(properties)
    if level is None:
        return None
    bbox = geometry_bbox(feature.get("geometry") or {})
    if bbox is None:
        return None

    unit_id = property_value(properties, f"GID_{level}")
    unit_name = property_value(properties, f"NAME_{level}")
    aliases = property_values(
        properties,
        f"NAME_{level}",
        f"VARNAME_{level}",
        f"NL_NAME_{level}",
    )
    iso = property_value(properties, "GID_0", "ISO", "COUNTRY")
    if not iso and unit_id:
        iso = unit_id.split(".", 1)[0]
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "iso": iso[:3].upper() if iso else "",
        "unit_level": level,
        "unit_id": unit_id,
        "unit_name": unit_name,
        "unit_name_aliases": aliases,
        "bbox_min_lon": min_lon,
        "bbox_min_lat": min_lat,
        "bbox_max_lon": max_lon,
        "bbox_max_lat": max_lat,
        "centroid_lon": (min_lon + max_lon) / 2,
        "centroid_lat": (min_lat + max_lat) / 2,
        "geometry_source_file": str(source_file),
    }


def load_gadm_index(
    gadm_dir: Path,
) -> Tuple[
    Dict[Tuple[str, str], Dict],
    Dict[Tuple[str, int, str], Optional[Dict]],
    Dict[Tuple[str, int, str], Optional[Dict]],
    Dict[Tuple[str, int], List[Tuple[str, Dict]]],
]:
    by_id: Dict[Tuple[str, str], Dict] = {}
    by_id_base: Dict[Tuple[str, int, str], Optional[Dict]] = {}
    by_name: Dict[Tuple[str, int, str], Optional[Dict]] = {}
    by_level_candidates: Dict[Tuple[str, int], List[Tuple[str, Dict]]] = {}
    candidate_keys = set()

    for path in find_geojson_files(gadm_dir):
        try:
            geojson = load_geojson(path)
        except (ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            print(f"Skipping {path}: {exc}")
            continue
        features = geojson.get("features", [])
        if geojson.get("type") == "Feature":
            features = [geojson]
        for feature in features:
            record = feature_record(feature, path)
            if not record:
                continue
            iso = record["iso"]
            unit_id = record["unit_id"]
            level = int(record["unit_level"])
            if unit_id:
                by_id[(iso, unit_id)] = record
                base_id = normalize_gadm_id_base(unit_id)
                if base_id and base_id != unit_id:
                    add_unique_index(by_id_base, (iso, level, base_id), record)

            aliases = record.get("unit_name_aliases") or [record["unit_name"]]
            for alias in aliases:
                for unit_name in name_variants(alias):
                    add_unique_index(by_name, (iso, level, unit_name), record)
                    candidate_key = (iso, level, unit_name, record["unit_id"])
                    if candidate_key in candidate_keys:
                        continue
                    candidate_keys.add(candidate_key)
                    by_level_candidates.setdefault((iso, level), []).append((unit_name, record))
    return by_id, by_id_base, by_name, by_level_candidates


def fuzzy_name_match(
    iso: str,
    level: int,
    names: List[str],
    by_level_candidates: Dict[Tuple[str, int], List[Tuple[str, Dict]]],
    threshold: float,
) -> Tuple[Optional[Dict], str, float]:
    candidates = by_level_candidates.get((iso, level), [])
    if not candidates or not names or threshold >= 1.0:
        return None, "no_match", 0.0

    best_record = None
    best_score = 0.0
    second_score = 0.0
    for name in names:
        if len(name) < 4:
            continue
        for candidate_name, record in candidates:
            score = SequenceMatcher(None, name, candidate_name).ratio()
            if score > best_score:
                second_score = best_score
                best_score = score
                best_record = record
            elif score > second_score:
                second_score = score

    if best_record and best_score >= threshold and best_score - second_score >= 0.02:
        return best_record, "name_level_fuzzy", best_score
    return None, "no_match", best_score


def match_admin_unit(
    row: pd.Series,
    by_id: Dict,
    by_id_base: Dict,
    by_name: Dict,
    by_level_candidates: Dict,
    fuzzy_name_threshold: float,
) -> Tuple[Optional[Dict], str, float]:
    iso = str(row.get("iso") or "").upper()
    unit_id = normalize_unit_id(row.get("unit_id"))
    unit_names = name_variants(row.get("unit_name"))
    unit_level = row.get("unit_level")
    try:
        level = int(unit_level)
    except (TypeError, ValueError):
        level = None

    if iso and unit_id and GID_RE.match(unit_id):
        record = by_id.get((iso, unit_id))
        if record:
            return record, "gadm_id", 1.0
        base_id = normalize_gadm_id_base(unit_id)
        record = by_id_base.get((iso, level, base_id)) if level is not None else None
        if record:
            return record, "gadm_id_base", 1.0

    if iso and level is not None and unit_names:
        for unit_name in unit_names:
            record = by_name.get((iso, level, unit_name))
            if record:
                return record, "name_level", 1.0
        record, method, score = fuzzy_name_match(
            iso=iso,
            level=level,
            names=unit_names,
            by_level_candidates=by_level_candidates,
            threshold=fuzzy_name_threshold,
        )
        if record:
            return record, method, score

    return None, "no_match", 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Join normalized EM-DAT administrative units with local GADM "
            "GeoJSON files to create prototype centroids and bounding boxes."
        )
    )
    parser.add_argument("--admin-units-csv", default=RECENT_EMDAT_ADMIN_UNITS_CSV)
    parser.add_argument("--gadm-dir", default=GADM_DATA_DIR)
    parser.add_argument("--output-csv", default=RECENT_EMDAT_ADMIN_UNIT_BBOXES_CSV)
    parser.add_argument(
        "--fuzzy-name-threshold",
        type=float,
        default=DEFAULT_FUZZY_NAME_THRESHOLD,
        help=(
            "Minimum similarity for fallback ADM name matching within the same "
            "country and administrative level. Use 1.0 to disable fuzzy matching."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report input paths and available GADM files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    admin_units_path = Path(args.admin_units_csv)
    gadm_dir = Path(args.gadm_dir)
    output_path = Path(args.output_csv)

    if not admin_units_path.exists():
        raise SystemExit(
            "Admin units CSV not found.\n"
            f"Expected path: {admin_units_path}\n\n"
            "Run analyze_recent_emdat_geocoding.py first."
        )

    gadm_files = find_geojson_files(gadm_dir)
    if args.dry_run:
        print(f"Admin units CSV: {admin_units_path}")
        print(f"GADM directory: {gadm_dir}")
        print(f"GADM GeoJSON files found: {len(gadm_files)}")
        for path in gadm_files[:20]:
            print(f"- {path}")
        if len(gadm_files) > 20:
            print(f"... {len(gadm_files) - 20} more")
        return

    if not gadm_files:
        raise SystemExit(
            "No GADM GeoJSON files found.\n"
            f"Expected directory: {gadm_dir}\n\n"
            "Download GADM country files in JSON/GeoJSON format and place them "
            "under data/gadm/, then rerun this command."
        )

    admin_units = pd.read_csv(admin_units_path)
    by_id, by_id_base, by_name, by_level_candidates = load_gadm_index(gadm_dir)

    rows = []
    for _, row in admin_units.iterrows():
        record, match_method, match_score = match_admin_unit(
            row,
            by_id=by_id,
            by_id_base=by_id_base,
            by_name=by_name,
            by_level_candidates=by_level_candidates,
            fuzzy_name_threshold=args.fuzzy_name_threshold,
        )
        output = row.to_dict()
        if record:
            output.update(
                {
                    "match_status": "matched",
                    "match_method": match_method,
                    "match_score": round(match_score, 4),
                    "matched_gadm_unit_id": record["unit_id"],
                    "matched_gadm_unit_name": record["unit_name"],
                    "bbox_min_lon": record["bbox_min_lon"],
                    "bbox_min_lat": record["bbox_min_lat"],
                    "bbox_max_lon": record["bbox_max_lon"],
                    "bbox_max_lat": record["bbox_max_lat"],
                    "centroid_lon": record["centroid_lon"],
                    "centroid_lat": record["centroid_lat"],
                    "geometry_source_file": record["geometry_source_file"],
                    "geometry_note": "centroid is bbox center for this prototype",
                }
            )
        else:
            output.update(
                {
                    "match_status": "unmatched",
                    "match_method": match_method,
                    "match_score": round(match_score, 4),
                    "matched_gadm_unit_id": "",
                    "matched_gadm_unit_name": "",
                    "bbox_min_lon": "",
                    "bbox_min_lat": "",
                    "bbox_max_lon": "",
                    "bbox_max_lat": "",
                    "centroid_lon": "",
                    "centroid_lat": "",
                    "geometry_source_file": "",
                    "geometry_note": "",
                }
            )
        rows.append(output)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = pd.DataFrame(rows)
    result.to_csv(output_path, index=False)

    matched = result["match_status"].eq("matched")
    print(f"Admin unit rows: {len(result)}")
    print(f"Matched rows: {int(matched.sum())} ({round(matched.mean() * 100, 2)}%)")
    print(
        "Matched events: "
        f"{result.loc[matched, 'emdat_disaster_id'].nunique()} / "
        f"{result['emdat_disaster_id'].nunique()}"
    )
    print(f"Output CSV: {output_path}")


if __name__ == "__main__":
    main()
