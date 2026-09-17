import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main as pipeline
import fetch_weather_batch as batch
import export_final_event_dataset as export
import run_release_pipeline as release
from support.constants import WEATHER_VARIABLES
from support.final_dataset_records import public_values, weather_fields
from support.utils import calculate_weather_summaries
from support.weather_data import normalize_daily, requested_dates, valid_day_counts, weather_number
from test_final_dataset_export import EVENT, START, csv_file, fixture, geo_row


PARAMETERS = {"start_date": "2018-02-04", "end_date": "2018-02-24", "latitude": -22.479,
              "longitude": -44.095, "timezone": "auto", "daily": WEATHER_VARIABLES}


def daily_data():
    return normalize_daily({"time": requested_dates(PARAMETERS), **{key: [0.0] * 21 for key in WEATHER_VARIABLES}},
                           PARAMETERS, provider="Open-Meteo", units={"snowfall_sum": "cm"}, time_basis="America/Sao_Paulo")


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload)


class WeatherNormalizationTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("No network in tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_missing_is_not_zero(self):
        for value in (None, "none", -999, -9999, float("nan"), float("inf"), True):
            self.assertIsNone(weather_number(value))
        self.assertEqual(weather_number(0), 0)
        self.assertEqual(weather_number(-5), -5)
        self.assertIsNone(weather_number(-5, nonnegative=True))

    def test_sparse_dates_are_aligned_with_explicit_gaps(self):
        raw = {"time": ["2018-02-05"], "rain_sum": [0], "temperature_2m_max": [-999]}
        result = normalize_daily(raw, PARAMETERS, provider="test", units={}, time_basis="UTC")
        self.assertEqual(len(result["time"]), 21)
        self.assertIsNone(result["rain_sum"][0])
        self.assertEqual(result["rain_sum"][1], 0)
        self.assertEqual(valid_day_counts(result)["rain_sum"], 1)

    def test_malformed_and_all_missing_series_are_not_successes(self):
        for raw in ({"time": ["2018-02-04"]}, {"time": ["2018-02-04"] * 2},
                    {"time": ["2018-02-04"], "rain_sum": [1, 2]}):
            self.assertIsNone(normalize_daily(raw, PARAMETERS, provider="test", units={}, time_basis="UTC"))

    def test_summaries_exclude_missing_values_and_event_day(self):
        daily = daily_data()
        daily["rain_sum"] = [None] * 21
        daily["rain_sum"][0] = 0
        daily["rain_sum"][1] = 2
        daily["rain_sum"][10] = 1000
        daily["temperature_2m_max"] = [None] * 21
        pre, post = calculate_weather_summaries(daily)
        self.assertEqual(pre["total_rainfall_mm"], 2)
        self.assertEqual(pre["valid_days"]["rain_sum"], 2)
        self.assertIsNone(post["total_rainfall_mm"])
        self.assertIsNone(pre["avg_max_temperature_c"])

    def test_open_meteo_preserves_units_provider_and_nulls(self):
        daily = daily_data()
        daily["rain_sum"][0] = None
        payload = {"daily": daily, "daily_units": {"snowfall_sum": "cm"}, "timezone": "America/Sao_Paulo"}
        with patch("requests.get", return_value=response(payload)) as get:
            result = pipeline.fetch_weather_data(PARAMETERS, EVENT, 1)
        self.assertEqual(result["provider"], "Open-Meteo")
        self.assertEqual(result["units"]["snowfall_sum"], "cm")
        self.assertIsNone(result["rain_sum"][0])
        self.assertEqual(get.call_args.kwargs["params"]["temperature_unit"], "celsius")

    def test_open_meteo_retry_and_invalid_payload_fallback(self):
        with patch("requests.get", side_effect=[response({}, 503), response({"daily": daily_data()})]) as get, \
                patch.object(pipeline.time, "sleep"), redirect_stdout(io.StringIO()):
            result = pipeline.fetch_weather_data(PARAMETERS, EVENT, 1, max_retries=1)
        self.assertIsNotNone(result)
        self.assertEqual(get.call_count, 2)
        with patch("requests.get", return_value=response({})), \
                patch.object(pipeline, "fetch_nasa_power_weather_data", return_value=None) as fallback, \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(pipeline.fetch_weather_data(PARAMETERS, EVENT, 1))
        fallback.assert_called_once()

    def test_nasa_total_precipitation_is_not_exported_as_rain(self):
        payload = {"properties": {"parameter": {"PRECTOTCORR": {"20180204": 10},
                                               "T2M_MAX": {"20180204": 0, "20180205": -888},
                                               "T2M_MIN": {"20180204": -999}}},
                   "header": {"fill_value": -888}}
        with patch("requests.get", return_value=response(payload)), redirect_stdout(io.StringIO()):
            daily = pipeline.fetch_nasa_power_weather_data(PARAMETERS, EVENT, 1)
        self.assertEqual(daily["precipitation_sum"][0], 10)
        self.assertEqual(daily["temperature_2m_max"][0], 0)
        self.assertIsNone(daily["temperature_2m_max"][1])
        self.assertTrue(all(value is None for key in ("rain_sum", "snowfall_sum", "temperature_2m_min") for value in daily[key]))
        final = public_values(weather_fields({"weather_data": {"daily_series": daily}}, START, []))
        self.assertEqual(final["daily_20_days_series"][0]["rain_sum"], "none")
        self.assertEqual(final["daily_20_days_series"][0]["temperature_2m_max"], 0)

    def test_empty_nasa_response_is_a_failure(self):
        with patch("requests.get", return_value=response({})), redirect_stdout(io.StringIO()):
            self.assertIsNone(pipeline.fetch_nasa_power_weather_data(PARAMETERS, EVENT, 1))


class WeatherBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.export_args, self.manifest, self.manifest_path = fixture(self.root)
        self.args = batch.parse_args(["--input-csv", str(self.root / "geo.csv"),
                                     "--causal-csv", str(self.root / "causal.csv"),
                                     "--satellite-dir", str(self.root / "satellite"),
                                     "--output-json", str(self.root / "collected/weather.json"),
                                     "--progress-csv", str(self.root / "collected/progress.csv"),
                                     "--sleep-seconds", "0"])
        self.events = batch.weather_events(pd.DataFrame([geo_row()]))

    def run_with(self, result):
        with patch.object(batch, "fetch_weather_data", return_value=result) as fetch, redirect_stdout(io.StringIO()):
            batch.run_batch(self.args, self.events)
        return fetch, batch.load_json(Path(self.args.output_json))

    def test_release_selection_does_not_read_or_require_context_files(self):
        for path in ("weather.json", "news.json", "summary.csv"):
            (self.root / path).unlink()
        selected = export.build_export(self.export_args, selection_only=True)[0]
        self.assertEqual(set(selected), {EVENT})
        self.assertNotIn("weather_data", selected[EVENT])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(batch.release_event_ids(self.args), {EVENT})

    def test_release_selection_excludes_no_image_and_no_chain_events(self):
        csv_file(self.root / "geo.csv", [geo_row(), geo_row("not-causal")])
        for day in self.manifest["days"]:
            day["sentinel_2"] = {"status": "no_scene", "available": False, "outputs": {}}
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(batch.release_event_ids(self.args), set())

    def test_current_complete_records_are_reused_but_coordinate_changes_are_not(self):
        fetch, records = self.run_with(daily_data())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(records[EVENT]["weather_retrieval_status"], "fetched")
        self.assertEqual(self.run_with(daily_data())[0].call_count, 0)
        self.events[0]["longitude"] += 1
        self.assertEqual(self.run_with(daily_data())[0].call_count, 1)

    def test_failed_records_retry_without_force(self):
        _, records = self.run_with(None)
        self.assertEqual(records[EVENT]["weather_retrieval_status"], "failed")
        fetch, records = self.run_with(daily_data())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(records[EVENT]["weather_retrieval_status"], "fetched")

    def test_partial_retry_is_explicit_and_preserves_prior_data_on_failure(self):
        partial = daily_data()
        partial["rain_sum"][0] = None
        _, records = self.run_with(partial)
        self.assertEqual(records[EVENT]["weather_retrieval_status"], "partial")
        self.assertEqual(self.run_with(None)[0].call_count, 0)
        self.args.retry_partial = True
        fetch, retried = self.run_with(None)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(retried, records)

    def test_old_version_and_force_do_not_reuse_cache(self):
        _, records = self.run_with(daily_data())
        records[EVENT]["weather_batch_version"] = "weather_batch_v1"
        batch.save_json(Path(self.args.output_json), records)
        self.assertEqual(self.run_with(daily_data())[0].call_count, 1)
        self.args.force = True
        self.assertEqual(self.run_with(daily_data())[0].call_count, 1)

    def test_retry_cannot_replace_complete_weather_with_less_coverage(self):
        _, original = self.run_with(daily_data())
        self.args.force = True
        partial = daily_data()
        partial["snowfall_sum"] = [None] * 21
        fetch, retried = self.run_with(partial)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(retried, original)

    def test_interrupt_keeps_already_checkpointed_events(self):
        self.events.append({**self.events[0], "event_id": "next", "longitude": 0})
        with patch.object(batch, "fetch_weather_data", side_effect=[daily_data(), KeyboardInterrupt]), \
                redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            batch.run_batch(self.args, self.events)
        saved = batch.load_json(Path(self.args.output_json))
        self.assertEqual(set(saved), {EVENT})
        self.assertEqual(saved[EVENT]["weather_retrieval_status"], "fetched")
        self.assertEqual(self.run_with(daily_data())[0].call_count, 1)

    def test_cache_reuses_identical_dates_and_coordinates_across_events(self):
        self.events.append({**self.events[0], "event_id": "same-point"})
        fetch, records = self.run_with(daily_data())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(records), 2)

    def test_corrupt_checkpoint_is_not_silently_overwritten(self):
        path = Path(self.args.output_json)
        path.parent.mkdir()
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError), patch.object(batch, "fetch_weather_data") as fetch:
            batch.run_batch(self.args, self.events)
        fetch.assert_not_called()
        self.assertEqual(path.read_text(), "broken")

    def test_atomic_write_keeps_previous_checkpoint_on_replace_failure(self):
        path = self.root / "atomic.json"
        path.write_text("original", encoding="utf-8")
        with patch.object(batch.os, "replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            batch.atomic_text(path, "new")
        self.assertEqual(path.read_text(), "original")
        self.assertFalse(list(self.root.glob(".atomic.json.*")))

    def test_dry_run_writes_nothing_and_never_calls_weather(self):
        self.args.dry_run = True
        self.args.release_events_only = True
        before = sorted(self.root.rglob("*"))
        with patch.object(batch, "parse_args", return_value=self.args), \
                patch.object(batch, "fetch_weather_data") as fetch, redirect_stdout(io.StringIO()):
            batch.main()
        fetch.assert_not_called()
        self.assertEqual(before, sorted(self.root.rglob("*")))

    def test_invalid_coordinates_review_sources_and_duplicate_ids(self):
        bad = geo_row("bad")
        bad["latitude"] = float("inf")
        review = geo_row("review")
        review["position_source"] = "llm_nominatim_review"
        self.assertEqual(batch.weather_events(pd.DataFrame([bad, review])), [])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            batch.weather_events(pd.DataFrame([geo_row(), geo_row()]))

    def test_missing_month_is_not_filled_automatically(self):
        row = geo_row()
        row["_llm_start_date"] = "2018"
        self.assertEqual(batch.weather_events(pd.DataFrame([row])), [])

    def test_saved_weather_exports_with_units_and_without_legacy_warning(self):
        _, data = self.run_with(daily_data())
        warnings = []
        result = public_values(weather_fields(data[EVENT], START, warnings))
        self.assertEqual(result["units"]["snowfall_sum"], "cm")
        self.assertEqual(result["pre_event_summary"]["mean_daily_rainfall_mm"], 0)
        self.assertFalse(warnings)

    def test_main_forwards_release_filter_without_changing_default(self):
        with patch.object(sys, "argv", ["main", "--weather-release-events-only"]):
            args = release.parse_args()
        weather = next(step for step in release.build_steps(args) if step.name == "weather")
        self.assertIn("--release-events-only", weather.commands[0])
        self.assertIn(args.normalized_causal_csv, weather.required_inputs)
        with patch.object(sys, "argv", ["main"]):
            args = release.parse_args()
        weather = next(step for step in release.build_steps(args) if step.name == "weather")
        self.assertNotIn("--release-events-only", weather.commands[0])


if __name__ == "__main__":
    unittest.main()
