import copy
import csv
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import export_final_event_dataset as export
import run_release_pipeline as release
from support import final_dataset_records as records
from support import final_dataset_satellite as satellite


EVENT = "2018-0040-BRA"
START = date(2018, 2, 14)
TIFF = b"II*\x00fixture-image-bytes"


def csv_file(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def geo_row(event_id=EVENT):
    return {"DisNo.": event_id, "Country": "Brazil", "Region": "Americas",
            "Disaster Type": "Flood", "latitude": -22.479, "longitude": -44.095,
            "position_source": "em-dat", "_llm_start_date": START.isoformat(),
            "Total Deaths": 0, "Total Affected": "", "Total Damage ('000 US$)": 1.25}


def causal_row(event_id=EVENT):
    return {"event_id": event_id, "causal_chain_parse_status": "parsed",
            "causal_chain_json": json.dumps({"causal_chain": [
                {"n_event": 1, "type_event": "Flood", "description": "Flooding occurred.",
                 "supporting_quote": "The river overflowed.", "private_extra": "never publish"}]}),
            "causal_chain_length": 999, "causal_chain_dropped_quote_steps": 0,
            "causal_chain_raw_response": "SECRET RAW RESPONSE", "causal_chain_prompt": "SECRET ARTICLE BODY"}


def weather_record():
    return {"event_id": EVENT, "start_date": START.isoformat(),
            "latitude": -22.479, "longitude": -44.095,
            "weather_data": {"provider": "Open-Meteo", "units": {"snowfall_sum": "cm"},
                             "daily_series": {"time": records.window_dates(START),
                                              "rain_sum": list(range(21)), "snowfall_sum": [0] * 21,
                                              "temperature_2m_max": [30] * 21, "temperature_2m_min": [20] * 21}}}


def news_record():
    return {"disaster_id": EVENT, "news_data": {
        "search_metadata": {"global_sources_queried": ["ReliefWeb"],
                            "regional_sources_queried": [], "fallback_sources_queried": []},
        "articles": [{"source": "ReliefWeb", "url": "https://example.org/article", "relevance_score": 8,
                      "relevance_reasons": ["hazard_in_title"], "relevance_penalties": [],
                      "title": "SECRET TITLE", "raw_text": "SECRET ARTICLE BODY"}]}}


def fixture(root):
    root = Path(root)
    event_dir = root / "satellite" / EVENT
    event_dir.mkdir(parents=True)
    image = event_dir / "image.tif"
    image.write_bytes(TIFF)
    days = [{"date": day, **{sensor: {"status": "no_scene", "available": False, "outputs": {}}
                             for sensor in satellite.SENSORS}}
            for day in records.window_dates(START)]
    days[10]["sentinel_2"] = {"status": "available", "available": True,
                               "scene": {"cloud_cover": 12}, "outputs": {"true_color_tif": str(image)}}
    manifest = {"event": {"event_id": EVENT, "start_date": START.isoformat(),
                          "latitude": -22.479, "longitude": -44.095},
                "bbox": [-44.2, -22.6, -44.0, -22.3],
                "temporal_window": {"days": 21, "from": days[0]["date"], "to": days[-1]["date"]},
                "days": days, "land_cover": {"status": "disabled"}}
    manifest_path = event_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    csv_file(root / "geo.csv", [geo_row()])
    csv_file(root / "causal.csv", [causal_row()])
    csv_file(root / "summary.csv", [{"event_id": EVENT, "summary_validation_status": "accepted",
                                     "event_summary": "A saved summary.", "usable_news_count": 1,
                                     "news_total_chars": 200, "news_input_quality": "limited",
                                     "summary_raw_response": "SECRET RAW SUMMARY"}])
    (root / "weather.json").write_text(json.dumps({EVENT: weather_record()}), encoding="utf-8")
    (root / "news.json").write_text(json.dumps({EVENT: news_record()}), encoding="utf-8")
    args = export.parse_args([
        "--geocoding-csv", str(root / "geo.csv"), "--causal-csv", str(root / "causal.csv"),
        "--weather-json", str(root / "weather.json"), "--news-json", str(root / "news.json"),
        "--summary-csv", str(root / "summary.csv"), "--satellite-dir", str(root / "satellite"),
        "--output-dir", str(root / "release"),
    ])
    return args, manifest, manifest_path


class RecordTests(unittest.TestCase):
    def test_missing_numbers_and_real_zero(self):
        value = records.public_values({"a": None, "b": math.nan, "c": "", "d": "NaN",
                                       "zero": 0, "false": False, "empty": [], "nested": [None]})
        self.assertEqual(value, {"a": "none", "b": "none", "c": "none", "d": "none",
                                 "zero": 0, "false": False, "empty": [], "nested": ["none"]})
        fields = records.public_values(records.event_fields(EVENT, geo_row(), START))
        self.assertEqual(fields["total_deaths"], 0)
        self.assertEqual(fields["total_affected"], "none")
        self.assertEqual(fields["total_damage_usd"], 1250.0)

    def test_no_invented_date_or_inflation_adjusted_damage(self):
        self.assertIsNone(records.event_date({"Start Year": 2020}))
        self.assertIsNone(records.event_fields(EVENT, {"Total Damage, Adjusted ('000 US$)": 99}, START)["total_damage_usd"])
        self.assertIsNone(records.number("-1", count=True))
        self.assertIsNone(records.number(True))

    def test_weather_means_exclude_event_day_and_missing_measurements(self):
        source = weather_record()
        source["weather_data"]["daily_series"]["rain_sum"][10] = 10000
        source["weather_data"]["daily_series"]["temperature_2m_max"][0] = -999
        notes = []
        result = records.weather_fields(source, START, notes)
        self.assertEqual(result["pre_event_summary"]["mean_daily_rainfall_mm"], 4.5)
        self.assertEqual(result["post_event_summary"]["mean_daily_rainfall_mm"], 15.5)
        self.assertEqual(result["pre_event_summary"]["mean_daily_max_temperature_c"], 30)
        self.assertIsNone(result["pre_event_summary"]["valid_days"])
        self.assertEqual(result["post_event_summary"]["valid_days"], 10)
        self.assertIn('"temperature_2m_max": 9', notes[0])
        self.assertIn("weather_valid_days_differ:pre_event_summary:", notes[0])
        self.assertEqual(result["daily_20_days_series"][10]["date"], START.isoformat())

    def test_valid_days_is_a_common_scalar_including_zero(self):
        for count in (10, 9, 0):
            with self.subTest(count=count):
                source = weather_record()
                for field in records.WEATHER_FIELDS:
                    source["weather_data"]["daily_series"][field][:10 - count] = [None] * (10 - count)
                notes = []
                result = records.public_values(records.weather_fields(source, START, notes))
                self.assertEqual(result["pre_event_summary"]["valid_days"], count)
                self.assertEqual(result["post_event_summary"]["valid_days"], 10)
                self.assertEqual(notes, [])
                if count == 0:
                    self.assertEqual(result["pre_event_summary"]["mean_daily_rainfall_mm"], "none")

    def test_weather_dates_are_aligned_and_fill_values_do_not_become_zero(self):
        source = weather_record()
        daily = source["weather_data"]["daily_series"]
        for key in daily:
            daily[key] = daily[key][1:]
        daily["rain_sum"][0] = -999
        output = records.public_values(records.weather_fields(source, START, []))
        self.assertEqual(len(output["daily_20_days_series"]), 21)
        self.assertEqual(output["daily_20_days_series"][0]["rain_sum"], "none")
        self.assertEqual(output["daily_20_days_series"][1]["rain_sum"], "none")
        self.assertEqual(output["pre_event_summary"]["valid_days"], "none")

    def test_legacy_provider_warning_and_explicit_nasa_snow(self):
        record = weather_record()
        del record["weather_data"]["provider"]
        notes = []
        records.weather_fields(record, START, notes)
        self.assertTrue(any("provider_unknown" in note for note in notes))
        record["weather_data"]["provider"] = "NASA POWER"
        result = records.weather_fields(record, START, [])
        self.assertIsNone(result["pre_event_summary"]["mean_daily_snowfall"])

    def test_bad_weather_arrays_fail(self):
        source = weather_record()
        source["weather_data"]["daily_series"]["rain_sum"] = []
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            records.weather_fields(source, START, [])

    def test_news_counts_only_final_deduplicated_articles_and_strips_text(self):
        source = news_record()
        articles = source["news_data"]["articles"]
        articles.append(dict(articles[0]))
        articles.append({"source": "DuckDuckGo Local", "url": "https://example.org/local"})
        result = records.news_fields(source, [])
        metadata = result["search_metadata"]
        self.assertEqual(metadata["total_articles_retrieved"], 2)
        self.assertEqual(metadata["global_sources"], [{"source": "ReliefWeb", "article_count": 1}])
        self.assertEqual(metadata["fallback_sources"], [{"source": "DuckDuckGo", "article_count": 1}])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unresolved_article_category_is_not_guessed(self):
        source = news_record()
        source["news_data"]["articles"][0]["source"] = "Unknown Publisher"
        notes = []
        result = records.news_fields(source, notes)["search_metadata"]
        self.assertEqual(result["global_sources"], [])
        self.assertEqual(result["total_articles_retrieved"], 1)
        self.assertIn("category_unresolved", notes[0])

    def test_causal_allowlist_retains_quotes_not_prompts_or_duplicate_chain(self):
        result = records.causal_fields(causal_row())
        self.assertEqual(result["causal_chain_length"], 1)
        self.assertEqual(result["steps"][0]["supporting_quote"], "The river overflowed.")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("private_extra", json.dumps(result))
        self.assertNotIn("total_causal_chain", result)

    def test_summary_retains_execution_metadata_not_news_metrics(self):
        notes = []
        row = {"event_summary": "unapproved", "summary_validation_status": "rejected_unrelated_news_admission",
               "llm_call_status": "called", "model_name": "saved-model", "summary_prompt_version": "v5",
               "usable_news_count": 2, "news_total_chars": 500, "news_input_quality": "limited"}
        result = records.public_values(records.summary_fields(row, notes))
        self.assertEqual(set(result), {"event_summary", "model_name", "llm_call_status",
                                      "summary_prompt_version", "summary_validation_status", "token_usage"})
        self.assertEqual(result["event_summary"], "none")
        for key in records.SUMMARY_TEXT:
            self.assertEqual(result[key], row[key])
        self.assertIn("accepted_summary_unavailable", notes)

    def test_news_metrics_centralized_without_merging_different_model_inputs(self):
        summary = {"news_count": "5", "summary_relevant_news_count": "4", "news_rejected_for_summary": "1",
                   "selected_news_count": "3", "usable_news_count": "2", "news_sources_count": "2",
                   "news_total_chars": "500", "news_input_quality": "limited"}
        causal = {**causal_row(), "news_count": "5", "causal_relevant_news_count": "2",
                  "news_rejected_for_causal_chain": "3", "selected_news_count": "2", "usable_news_count": "1",
                  "news_sources_count": "1", "news_total_chars": "250", "news_input_quality": "limited"}
        metadata = records.news_fields(news_record(), [], summary_row=summary, causal_row=causal)["search_metadata"]
        for stage, row, relevant, rejected in (("summary_input", summary, 4, 1), ("causal_chain_input", causal, 2, 3)):
            for key in records.NEWS_NUMBERS:
                self.assertEqual(metadata[stage][key], int(row[key]))
            self.assertEqual(metadata[stage]["relevant_news_count"], relevant)
            self.assertEqual(metadata[stage]["rejected_news_count"], rejected)
            self.assertEqual(metadata[stage]["news_input_quality"], row["news_input_quality"])
        for block in (records.causal_fields(causal), records.summary_fields(summary, [])):
            self.assertFalse(set(block) & (set(records.NEWS_NUMBERS) | {
                "causal_relevant_news_count", "news_rejected_for_causal_chain", "summary_relevant_news_count",
                "news_rejected_for_summary", "news_input_quality", "usable_news", "total_chars"}))

    def test_missing_articles_do_not_discard_saved_model_input_metrics(self):
        notes = []
        output = records.public_values(records.news_fields(None, notes, summary_row={"usable_news_count": 0}))
        metadata = output["search_metadata"]
        self.assertEqual(metadata["total_articles_retrieved"], "none")
        self.assertEqual(metadata["filtered_final_articles"], "none")
        self.assertEqual(metadata["causal_chain_input"], "none")
        self.assertEqual(metadata["summary_input"]["usable_news_count"], 0)
        self.assertEqual(metadata["summary_input"]["news_total_chars"], "none")
        self.assertIn("news_unavailable", notes)
        self.assertIsNone(records.news_fields(None, []))

    def test_token_usage_is_saved_only_not_estimated_or_imputed(self):
        row = {**causal_row(), "input_tokens": "100", "output_tokens": "0", "total_tokens": "100"}
        for builder in (records.causal_fields, lambda value: records.summary_fields(value, [])):
            usage = builder(row)["token_usage"]
            self.assertEqual([usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")], [100, 0, 100])
            self.assertEqual(usage["input_method"], "recorded")
            result = records.public_values(builder({**causal_row(), "news_total_chars": 2000, "max_new_tokens": 512}))
            self.assertEqual(set(result["token_usage"].values()), {"none"})
        self.assertEqual(set(records.token_usage({"input_tokens": -1, "output_tokens": "NaN", "total_tokens": True}).values()),
                         {None})

    def test_duplicate_csv_and_json_ids_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.csv"
            csv_file(path, [geo_row(), geo_row()])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                records.read_records(path)
            path = Path(folder) / "records.json"
            path.write_text('{"x": {}, "x": {}}')
            with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
                records.read_records(path)


class SatelliteExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args, self.manifest, self.path = fixture(self.root)

    def fields(self, **kwargs):
        return satellite.satellite_fields(self.manifest, EVENT, geo_row(), START,
                                          self.path.parent, self.root, **kwargs)

    def test_paths_categories_and_21_slots(self):
        result, assets = self.fields()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["availability"]["sentinel_2"][0], "no_scene")
        self.assertEqual(len(result["availability"]["sentinel_2"]), 21)
        self.assertEqual(result["sentinel_2_rgb"][0]["tif"],
                         "../images/2018-0040-BRA/sentinel_2_rgb/day_10_2018-02-14.tif")
        self.assertEqual(len(assets), 1)

    def test_partial_kept_explicitly_or_excluded_by_flag(self):
        self.manifest["days"][0]["sentinel_3_slstr"]["status"] = "error"
        result, _ = self.fields()
        self.assertEqual(result["status"], "partial")
        with self.assertRaisesRegex(satellite.SatelliteRejected, "partial"):
            self.fields(require_complete=True)

    def test_missing_and_corrupt_files_rejected(self):
        image = self.path.parent / "image.tif"
        image.write_bytes(b"error HTML")
        with self.assertRaisesRegex(satellite.SatelliteRejected, "signature"):
            self.fields()
        image.unlink()
        with self.assertRaisesRegex(satellite.SatelliteRejected, "missing_or_empty"):
            self.fields()

    def test_masks_do_not_qualify_as_event_imagery(self):
        slot = self.manifest["days"][10]["sentinel_2"]
        slot["outputs"] = {"data_mask_tif": slot["outputs"]["true_color_tif"]}
        with self.assertRaisesRegex(satellite.SatelliteRejected, "no_satellite_images"):
            self.fields()

    def test_event_date_coordinates_and_window_must_match(self):
        original = copy.deepcopy(self.manifest)
        for field, value in (("event_id", "wrong"), ("start_date", "2000-01-01"), ("latitude", 0)):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(original)
                self.manifest["event"][field] = value
                with self.assertRaises(satellite.SatelliteRejected):
                    self.fields()
        self.manifest = copy.deepcopy(original)
        self.manifest["days"][0]["date"] = self.manifest["days"][1]["date"]
        with self.assertRaisesRegex(satellite.SatelliteRejected, "window"):
            self.fields()

    def test_manifest_cannot_exfiltrate_files_outside_event(self):
        external = self.root / "private.tif"
        external.write_bytes(TIFF)
        self.manifest["days"][10]["sentinel_2"]["outputs"]["true_color_tif"] = str(external)
        with self.assertRaisesRegex(satellite.SatelliteRejected, "outside_event"):
            self.fields()

    def test_all_sensor_products_and_worldcover_are_preserved(self):
        self.manifest["band_sets"] = {"sentinel_2_raw_bands": ["B02", "B03"],
                                      "sentinel_2_false_color_order": ["B12", "B08", "B04"],
                                      "sentinel_1_bands": ["VV", "VH"],
                                      "sentinel_3_thermal_bands": ["S7", "S8", "S9", "F1", "F2"]}
        for sensor, products in satellite.PRODUCTS.items():
            outputs = {}
            for key, (_, extension) in products.items():
                path = self.path.parent / (sensor + "_" + key + "." + extension)
                path.write_bytes(TIFF if extension == "tif" else b"\x89PNG\r\n\x1a\nfixture")
                outputs[key] = str(path)
            self.manifest["days"][10][sensor] = {"status": "available", "available": True, "outputs": outputs}
        cover = self.path.parent / "cover.tif"
        cover.write_bytes(TIFF)
        self.manifest["land_cover"] = {"status": "available", "available": True, "year": 2020,
                                        "outputs": {"worldcover_tif": str(cover)}}
        result, assets = self.fields()
        self.assertEqual(len(assets), 11)
        self.assertEqual(result["sentinel_1_sar"][0]["bands"], ["VV", "VH"])
        self.assertEqual(result["sentinel_3_thermal"][0]["quantity"], "brightness_temperature")
        self.assertEqual(result["worldcover"]["reference_year"], 2020)
        self.assertIn("png", result["sentinel_2_false_color"][0])

    def test_worldcover_alone_does_not_qualify(self):
        self.manifest["days"][10]["sentinel_2"] = {"status": "no_scene", "available": False, "outputs": {}}
        self.manifest["land_cover"] = {"status": "available", "available": True, "year": 2020,
                                        "outputs": {"worldcover_tif": str(self.path.parent / "image.tif")}}
        with self.assertRaisesRegex(satellite.SatelliteRejected, "no_satellite_images"):
            self.fields()


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args, self.manifest, self.path = fixture(self.root)

    def test_build_and_copy_round_trip_without_network(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")), redirect_stdout(io.StringIO()):
            result = export.build_export(self.args)
            export.write_package(self.args, result)
            export.write_package(self.args, result)
        package = Path(self.args.output_dir)
        dataset = records.read_json(package / "data/dataset.json")
        event = dataset[EVENT]
        self.assertEqual(event["total_affected"], "none")
        self.assertEqual(event["total_deaths"], 0)
        self.assertEqual(event["total_damage_usd"], 1250.0)
        self.assertNotIn("SECRET", json.dumps(dataset))
        self.assertNotIn("null", json.dumps(dataset))
        self.assertNotIn("total_causal_chain", event["causal_chain"])
        self.assertEqual(event["weather_data"]["pre_event_summary"]["valid_days"], 10)
        self.assertEqual(event["news_data"]["search_metadata"]["summary_input"]["usable_news_count"], 1)
        self.assertEqual(event["news_data"]["search_metadata"]["summary_input"]["news_total_chars"], 200)
        self.assertEqual(event["summary"]["event_summary"], "A saved summary.")
        self.assertEqual(event["summary"]["summary_validation_status"], "accepted")
        self.assertNotIn("news_input_quality", event["summary"])
        self.assertNotIn("news_count", event["causal_chain"])
        manifest = records.read_json(package / "package_manifest.json")
        self.assertEqual(manifest["status"], "complete")
        entry = manifest["assets"][0]
        self.assertEqual(export.file_hash(package / entry["path"]), entry["sha256"])
        self.assertEqual((self.path.parent / "image.tif").read_bytes(), TIFF)

    def test_only_real_intersection_selected_not_legacy_images(self):
        csv_file(self.root / "geo.csv", [geo_row(), {**geo_row("no-coords"), "latitude": ""}, geo_row("no-chain")])
        csv_file(self.root / "causal.csv", [causal_row(), causal_row("no-coords")])
        result = export.build_export(self.args)
        self.assertEqual(list(result[0]), [EVENT])
        self.assertEqual([row["selection_status"] for row in result[2]],
                         ["selected", "missing_accepted_coordinates", "no_valid_causal_chain"])

    def test_missing_optional_files_require_explicit_permission(self):
        (self.root / "weather.json").unlink()
        with self.assertRaises(FileNotFoundError):
            export.build_export(self.args)
        self.args.allow_missing_context = True
        result = export.build_export(self.args)
        self.assertEqual(result[0][EVENT]["weather_data"], "none")

    def test_weather_from_other_coordinates_is_not_silently_joined(self):
        weather = weather_record()
        weather["latitude"] = 0
        (self.root / "weather.json").write_text(json.dumps({EVENT: weather}))
        result = export.build_export(self.args)
        self.assertEqual(result[0][EVENT]["weather_data"], "none")
        self.assertIn("weather_json_event_mismatch", result[3][EVENT])

    def test_unknown_requested_event_rejected(self):
        self.args.event_id = ["absent"]
        with self.assertRaisesRegex(ValueError, "Unknown event"):
            export.build_export(self.args)

    def test_changed_selection_or_input_requires_new_directory(self):
        with redirect_stdout(io.StringIO()):
            result = export.build_export(self.args)
            export.write_package(self.args, result)
            self.args.require_complete = True
            with self.assertRaisesRegex(ValueError, "changed"):
                export.write_package(self.args, export.build_export(self.args))

    def test_unmanaged_folder_and_source_folder_not_overwritten(self):
        destination = Path(self.args.output_dir)
        destination.mkdir()
        (destination / "keep.txt").write_text("user file")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "unmanaged"):
            export.write_package(self.args, export.build_export(self.args))
        self.assertEqual((destination / "keep.txt").read_text(), "user file")
        self.args.output_dir = str(self.root)
        with self.assertRaisesRegex(ValueError, "separate release"):
            export.write_package(self.args, export.build_export(self.args))

    def test_changed_source_during_copy_leaves_incomplete_package(self):
        result = export.build_export(self.args)
        original = export.copy_asset

        def mutate(asset, target, expected_hash, staging_dir):
            asset.source.write_bytes(TIFF + b"changed")
            original(asset, target, expected_hash, staging_dir)

        with patch.object(export, "copy_asset", side_effect=mutate), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                export.write_package(self.args, result)
        destination = Path(self.args.output_dir)
        self.assertFalse((destination / "data/dataset.json").exists())
        self.assertEqual(records.read_json(destination / "package_manifest.json")["status"], "incomplete")

    def test_dry_run_writes_nothing(self):
        self.args.dry_run = True
        before = sorted(str(path) for path in self.root.rglob("*"))
        with patch.object(export, "parse_args", return_value=self.args), redirect_stdout(io.StringIO()):
            export.main()
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))

    def test_resume_after_interrupted_copy_keeps_staging_outside_public_package(self):
        result = export.build_export(self.args)
        with patch.object(export, "copy_asset", side_effect=KeyboardInterrupt), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                export.write_package(self.args, result)
        stale = self.root / ".dataset-export-interrupted"
        stale.write_bytes(b"incomplete image")
        with redirect_stdout(io.StringIO()):
            export.write_package(self.args, result)
        self.assertEqual(records.read_json(Path(self.args.output_dir) / "package_manifest.json")["status"], "complete")
        self.assertTrue(stale.exists())
        self.assertFalse(any(path.name.startswith(".dataset-export-") for path in Path(self.args.output_dir).rglob("*")))

    def test_limit_selects_eligible_events_not_first_input_rows(self):
        csv_file(self.root / "geo.csv", [geo_row("ineligible"), geo_row()])
        self.args.limit = 1
        self.assertEqual(list(export.build_export(self.args)[0]), [EVENT])

    def old_package(self):
        result = export.build_export(self.args)
        with patch.object(export, "SCHEMA_VERSION", "environmental-causal-release-v1"), redirect_stdout(io.StringIO()):
            export.write_package(self.args, result)
        destination = Path(self.args.output_dir)
        dataset = records.read_json(destination / "data/dataset.json")
        event = dataset[EVENT]
        event["weather_data"]["pre_event_summary"]["valid_days"] = dict.fromkeys(records.WEATHER_FIELDS, 10)
        event["summary"]["usable_news"] = 1
        (destination / "data/dataset.json").write_bytes(export.json_bytes(dataset))
        manifest = records.read_json(destination / "package_manifest.json")
        manifest["dataset_sha256"] = export.file_hash(destination / "data/dataset.json")
        (destination / "package_manifest.json").write_bytes(export.json_bytes(manifest))
        return result, destination, manifest

    def test_schema_refresh_is_explicit_and_does_not_copy_images(self):
        result, destination, old = self.old_package()
        image = destination / old["assets"][0]["path"]
        before = image.stat().st_mtime_ns
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "refresh-metadata"):
            export.write_package(self.args, result)
        self.args.refresh_metadata = True
        with patch.object(export, "copy_asset", side_effect=AssertionError("Must not copy")), redirect_stdout(io.StringIO()):
            export.write_package(self.args, result)
            export.write_package(self.args, result)
        manifest = records.read_json(destination / "package_manifest.json")
        self.assertEqual(manifest["schema_version"], export.SCHEMA_VERSION)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["assets"], old["assets"])
        self.assertEqual(manifest["input_sha256"], old["input_sha256"])
        self.assertEqual(image.stat().st_mtime_ns, before)
        self.assertEqual(image.read_bytes(), TIFF)
        self.assertEqual(export.file_hash(destination / "data/dataset.json"), manifest["dataset_sha256"])
        event = records.read_json(destination / "data/dataset.json")[EVENT]
        self.assertEqual(event["weather_data"]["pre_event_summary"]["valid_days"], 10)
        self.assertNotIn("usable_news", event["summary"])

    def test_refresh_cannot_change_selection_inputs_or_assets(self):
        result, destination, old = self.old_package()
        self.args.refresh_metadata = True
        original_manifest = (destination / "package_manifest.json").read_bytes()
        for field, changed in (("event_ids", ["another-event"]), ("assets", []),
                               ("input_sha256", {}), ("schema_version", "unknown-schema")):
            with self.subTest(field=field):
                altered = {**old, field: changed}
                (destination / "package_manifest.json").write_bytes(export.json_bytes(altered))
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "changed"):
                    export.write_package(self.args, result)
        (destination / "package_manifest.json").write_bytes(original_manifest)

    def test_refresh_refuses_manual_json_edits_and_missing_images(self):
        result, destination, old = self.old_package()
        self.args.refresh_metadata = True
        dataset_path = destination / "data/dataset.json"
        saved = dataset_path.read_bytes()
        dataset_path.write_bytes(saved + b" ")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Existing dataset differs"):
            export.write_package(self.args, result)
        dataset_path.write_bytes(saved)
        (destination / old["assets"][0]["path"]).unlink()
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "missing images"):
            export.write_package(self.args, result)
        self.assertEqual(dataset_path.read_bytes(), saved)
        self.assertEqual(records.read_json(destination / "package_manifest.json"), old)

    def test_refresh_detects_corrupt_images_without_overwriting_them(self):
        result, destination, old = self.old_package()
        self.args.refresh_metadata = True
        image = destination / old["assets"][0]["path"]
        image.write_bytes(b"corrupt image")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Existing asset differs"):
            export.write_package(self.args, result)
        self.assertEqual(image.read_bytes(), b"corrupt image")
        self.assertEqual(records.read_json(destination / "package_manifest.json")["status"], "incomplete")
        self.assertEqual(export.file_hash(destination / "data/dataset.json"), old["dataset_sha256"])

    def test_interrupted_metadata_refresh_can_resume_without_copying(self):
        result, destination, old = self.old_package()
        self.args.refresh_metadata = True
        original = export.atomic_write

        def interrupt(path, content, staging_dir=None):
            if path.name == "dataset.json":
                raise KeyboardInterrupt
            return original(path, content, staging_dir)

        with patch.object(export, "atomic_write", side_effect=interrupt), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                export.write_package(self.args, result)
        self.assertEqual(records.read_json(destination / "package_manifest.json")["status"], "incomplete")
        self.assertEqual(export.file_hash(destination / "data/dataset.json"), old["dataset_sha256"])
        with patch.object(export, "copy_asset", side_effect=AssertionError("Must not copy")), redirect_stdout(io.StringIO()):
            export.write_package(self.args, result)
        self.assertEqual(records.read_json(destination / "package_manifest.json")["status"], "complete")

    def test_refresh_requires_an_existing_package(self):
        self.args.refresh_metadata = True
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "existing managed package"):
            export.write_package(self.args, export.build_export(self.args))
        self.assertFalse(Path(self.args.output_dir).exists())


class ReleaseIntegrationTests(unittest.TestCase):
    def test_final_and_all_include_package_without_changing_csv_step(self):
        with patch.object(sys, "argv", ["release", "--release-limit", "1", "--release-output-dir", "results/preview"]):
            args = release.parse_args()
        steps = release.build_steps(args)
        self.assertEqual([s.name for s in release.selected_steps(steps, ["final"])],
                         ["final-dataset", "release-package"])
        self.assertEqual(release.expand_requested_steps(["all"])[-1], "release-package")
        package = next(step for step in steps if step.name == "release-package")
        command = package.commands[0]
        self.assertEqual(command[command.index("--limit") + 1], "1")
        self.assertEqual(command[command.index("--output-dir") + 1], "results/preview")
        self.assertIn(args.weather_json, package.required_inputs)
        self.assertNotIn("--refresh-metadata", command)

    def test_release_forwards_explicit_metadata_refresh(self):
        with patch.object(sys, "argv", ["release", "--release-refresh-metadata", "--release-recover-tokens"]):
            args = release.parse_args()
        steps = release.build_steps(args)
        package = next(step for step in steps if step.name == "release-package")
        self.assertIn("--refresh-metadata", package.commands[0])
        self.assertIn("--recover-tokens", package.commands[0])


if __name__ == "__main__":
    unittest.main()
