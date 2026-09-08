import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.transform import from_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import assemble_final_event_dataset as assemble
import fetch_multimodal_satellite_batch as batch
import prepare_satellite_batch_input as prepare
import run_release_pipeline as release
from support import multimodal_satellite_engine as engine
from support import satellite_engine as shared
from support import worldcover


class InputTests(unittest.TestCase):
    def test_all_types_and_exclusion_audit(self):
        frame = pd.DataFrame({
            "DisNo.": ["flood", "fire", "road", "missing", "range", "old", "date"],
            "Country": ["Italy"] * 7,
            "Disaster Type": ["Flood", "Wildfire", "Road", "Flood", "Flood", "Storm", "Drought"],
            "Start Year": [2020] * 5 + [2010, 2020],
            "Start Month": [1] * 6 + [None], "Start Day": [15] * 7,
            "latitude": [42, 42, 42, None, 999, 42, 42], "longitude": [12] * 7,
        })
        selected, audit = prepare.satellite_input_with_audit(
            frame, disaster_type="", min_start_date="2014-04-03",
        )
        self.assertEqual(selected.emdat_disaster_id.tolist(), ["flood", "fire", "road"])
        self.assertEqual(audit.selection_status.tolist(), [
            "selected", "selected", "selected", "missing_coordinates", "invalid_coordinates",
            "before_start_date", "missing_or_invalid_date",
        ])
        self.assertEqual(len(prepare.build_satellite_input(frame, disaster_type="Flood")), 1)

    def test_duplicate_ids_fail(self):
        frame = pd.DataFrame({"event_id": ["x", "x"], "country": ["IT"] * 2,
                              "disaster_type": ["Flood"] * 2, "start_date": ["2020-01-01"] * 2,
                              "latitude": [42] * 2, "longitude": [12] * 2})
        with self.assertRaisesRegex(SystemExit, "Duplicate"):
            prepare.build_satellite_input(frame)

    def test_release_runs_general_not_flood_specific(self):
        with patch.object(sys, "argv", ["release"]):
            args = release.parse_args()
        steps = {step.name: step for step in release.build_steps(args)}
        self.assertEqual(release.expand_requested_steps(["satellite"]), ["satellite-input", "satellite-general"])
        self.assertIn("satellite-general", release.expand_requested_steps(["all"]))
        self.assertNotIn("satellite-flood", release.expand_requested_steps(["all"]))
        command = steps["satellite-general"].commands[0]
        self.assertEqual(command[command.index("--window-days") + 1], "10")
        self.assertEqual(command[command.index("--limit") + 1], "0")
        self.assertEqual(command[command.index("--disaster-type") + 1], "")
        self.assertEqual(args.satellite_ranking_csv, "")

    def test_full_input_dry_run_makes_no_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.csv"
            pd.DataFrame({"emdat_disaster_id": ["flood", "wildfire"], "country": ["Italy"] * 2,
                          "disaster_type": ["Flood", "Wildfire"], "start_date": ["2020-01-01"] * 2,
                          "latitude": [42, 43], "longitude": [12, 13]}).to_csv(path, index=False)
            with patch.object(sys, "argv", ["batch", "--events-csv", str(path), "--limit", "0", "--dry-run"]):
                args = batch.parse_args()
            with patch.object(batch, "run_multimodal_satellite_event") as run:
                batch.run_batch(args)
            run.assert_not_called()
            self.assertEqual(len(batch.select_events(args)), 2)

    def test_final_join_preserves_general_fields_and_event_count(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            geo = root / "geo.csv"
            sat = root / "sat.csv"
            pd.DataFrame({"DisNo.": ["flood", "fire", "excluded"], "latitude": [42, 43, None],
                          "longitude": [12, 13, None]}).to_csv(geo, index=False)
            pd.DataFrame({"event_id": ["flood", "fire", "fire"],
                          "status": ["completed", "no_data", "skipped_existing"],
                          "manifest_status": ["completed", "no_data", "no_data"],
                          "sentinel_2_available_days": [2, 0, 0],
                          "has_any_satellite_data": [True, False, False],
                          "land_cover_product": ["ESA WorldCover"] * 3}).to_csv(sat, index=False)
            with patch.object(sys, "argv", ["assemble"]):
                args = assemble.parse_args()
            args.geocoding_csv = str(geo)
            args.satellite_summary_csv = str(sat)
            for name in ("base_csv", "weather_progress_csv", "news_progress_csv", "summary_csv", "causal_csv"):
                setattr(args, name, str(root / "missing.csv"))
            master, summary = assemble.assemble_final_dataset(args)
            self.assertEqual(len(master), 3)
            self.assertEqual(master.loc[0, "satellite_sentinel_2_available_days"], "2")
            self.assertEqual(master.loc[0, "satellite_land_cover_product"], "ESA WorldCover")
            counts = summary.set_index("metric")["count"]
            self.assertEqual(counts["events_with_satellite_images"], 1)
            self.assertEqual(counts["satellite_no_data_records"], 1)
            self.assertEqual(counts["satellite_completed_or_existing"], 1)


class ClientTests(unittest.TestCase):
    def test_retry_after_is_milliseconds(self):
        client = shared.SentinelHubClient("test", "test")
        client._access_token = "test"
        response = Mock(status_code=429, headers={"Retry-After": "2000"}, ok=False)
        success = Mock(status_code=200, ok=True)
        with patch.object(client.session, "post", side_effect=[response, success]) as post, \
                patch.object(shared.time, "sleep") as sleep:
            self.assertIs(client._post("test", {}, "test", "test"), success)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(post.call_count, 2)
        client.session.close()

    def test_network_retry_and_expired_token(self):
        client = shared.SentinelHubClient("test", "test")
        client._access_token = "expired"
        expired = Mock(status_code=401, ok=False)
        token = Mock(status_code=200, ok=True)
        token.json.return_value = {"access_token": "fresh"}
        success = Mock(status_code=200, ok=True)
        with patch.object(client.session, "post", side_effect=[requests.Timeout(), expired, token, success]), \
                patch.object(shared.time, "sleep"):
            self.assertIs(client._post("test", {}, "test", "test"), success)
        self.assertEqual(client._access_token, "fresh")
        client.session.close()

    def test_catalog_pages_and_daily_cache(self):
        client = engine.DailyCatalogClient("test", "test", 30, ["2020-01-01", "2020-01-21"])
        pages = []
        for day, token in [(1, 100), (2, None)]:
            page = Mock()
            page.json.return_value = {"features": [{"properties": {"datetime": f"2020-01-{day:02d}T12:00:00Z"}}],
                                      "context": {"next": token}}
            pages.append(page)
        with patch.object(client, "_post", side_effect=pages) as post:
            self.assertEqual(len(client.catalog_search("sensor", [0, 0, 1, 1], "2020-01-01", "2020-01-01")), 1)
            self.assertEqual(len(client.catalog_search("sensor", [0, 0, 1, 1], "2020-01-02", "2020-01-02")), 1)
            self.assertEqual(post.call_count, 2)
        client.session.close()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.event = shared.SatelliteEvent(event_id="2020-TEST-ITA", start_date="2020-01-15",
                                          latitude=42, longitude=12, disaster_type="Flood", country="Italy")
        self.config = engine.MultimodalSatelliteConfig(window_days=0, include_land_cover=False,
                                                       include_sentinel_3=False)
        self.credentials = patch.object(engine, "require_copernicus_credentials", return_value=("test", "test"))
        self.credentials.start()
        self.addCleanup(self.credentials.stop)

    def run_event(self, config=None, force=False):
        return engine.run_multimodal_satellite_event(self.event, config or self.config, str(self.root), force=force)

    def test_window_cloud_and_acquisition(self):
        dates = engine.analysis_dates("2020-01-15", 10)
        self.assertEqual((len(dates), dates[0], dates[-1]), (21, "2020-01-05", "2020-01-25"))
        clear = {"properties": {"eo:cloud_cover": 0, "datetime": "2020-01-15T12:00:00Z"}}
        cloudy = {"properties": {"eo:cloud_cover": 20, "datetime": "2020-01-15T10:00:00Z"}}
        client = Mock()
        client.catalog_search.return_value = [cloudy, clear]
        self.assertIs(engine.select_daily_s2_scene(client, [0, 0, 1, 1], "2020-01-15", 100), clear)
        self.assertEqual(engine.acquisition_time_range(clear), ("2020-01-15T11:59:59Z", "2020-01-15T12:00:01Z"))
        with self.assertRaises(ValueError):
            engine.MultimodalSatelliteConfig(output_resolution_m=1)

    def test_resume_error_without_redownloading_success(self):
        image = self.root / "download.tif"
        image.write_bytes(b"synthetic image")
        available = {"status": "available", "available": True, "outputs": {"image": str(image)}}
        with patch.object(engine, "fetch_sensor_day", side_effect=[available, shared.SentinelHubRequestError("temporary")]):
            manifest = self.run_event()
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["quality_summary"]["error_days"]["sentinel_1"], 1)
        with patch.object(engine, "fetch_sensor_day", return_value={**engine.empty_sensor_slot(), "status": "no_scene"}) as fetch:
            manifest = self.run_event()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[3], "sentinel_1")
        self.assertEqual(manifest["status"], "completed")
        image.unlink()
        self.assertFalse(engine.manifest_complete(manifest))

    def test_no_scene_is_resolved_not_a_download_error(self):
        no_scene = {**engine.empty_sensor_slot(), "status": "no_scene"}
        with patch.object(engine, "fetch_sensor_day", return_value=no_scene):
            manifest = self.run_event()
        self.assertEqual(manifest["status"], "no_data")
        self.assertTrue(engine.manifest_complete(manifest))
        with patch.object(engine, "fetch_sensor_day") as fetch:
            self.run_event()
        fetch.assert_not_called()
        with self.assertRaisesRegex(ValueError, "different configuration"):
            self.run_event(replace(self.config, window_days=1))

    def test_interrupt_saves_completed_sensor(self):
        no_scene = {**engine.empty_sensor_slot(), "status": "no_scene"}
        with patch.object(engine, "fetch_sensor_day", side_effect=[no_scene, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.run_event()
        manifest = json.loads((self.root / self.event.event_id / "manifest.json").read_text())
        self.assertEqual(manifest["days"][0]["sentinel_2"]["status"], "no_scene")
        self.assertEqual(manifest["days"][0]["sentinel_1"]["status"], "pending")

    def test_quota_stops_immediately_and_records_error(self):
        with patch.object(engine, "fetch_sensor_day", side_effect=shared.SentinelHubRequestError("quota", 429)) as fetch:
            with self.assertRaises(shared.SentinelHubRequestError):
                self.run_event()
        self.assertEqual(fetch.call_count, 1)
        manifest = json.loads((self.root / self.event.event_id / "manifest.json").read_text())
        self.assertEqual(manifest["days"][0]["sentinel_2"]["status"], "error")

    def test_summary_preserves_no_data_on_resume(self):
        manifest = {"status": "no_data", "quality_summary": {"available_days": {"sentinel_2": 0}}}
        row = batch.summary_row("now", self.event, "skipped_existing", 1, manifest=manifest)
        self.assertFalse(row["has_any_satellite_data"])
        self.assertEqual(row["manifest_status"], "no_data")
        self.assertEqual(set(row), set(batch.SUMMARY_FIELDS))


class WorldCoverTests(unittest.TestCase):
    def test_tile_names_and_reference_years(self):
        urls = worldcover.worldcover_tile_urls([-0.1, -0.1, 0.1, 0.1], 2021)
        self.assertEqual(len(urls), 4)
        self.assertTrue(any("S03W003_Map.tif" in url for url in urls))
        self.assertTrue(any("N00E000_Map.tif" in url for url in urls))
        self.assertEqual(worldcover.worldcover_year("2014-01-01"), 2020)
        self.assertEqual(worldcover.worldcover_year("2025-01-01"), 2021)

    def test_crop_preserves_classes_and_writes_geotiff(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.tif"
            transform = from_bounds(0, 0, 3, 3, 3, 3)
            with rasterio.open(source, "w", driver="GTiff", width=3, height=3, count=1,
                               dtype="uint8", crs="EPSG:4326", transform=transform, nodata=0) as image:
                image.write(np.array([[10, 20, 30], [40, 50, 60], [70, 80, 90]], dtype="uint8"), 1)
            response = Mock(status_code=200)
            with patch.object(requests.Session, "head", return_value=response):
                pixels, cropped_transform, urls = worldcover.worldcover_pixels([str(source)], [1, 1, 2, 2], 2, 2)
            self.assertTrue(np.all(pixels == 50))
            output = Path(folder) / "crop.tif"
            with patch.object(worldcover, "worldcover_pixels", return_value=(pixels, cropped_transform, urls)):
                record = worldcover.download_worldcover("2014-01-01", [1, 1, 2, 2], output, 2, 2)
            self.assertEqual(record["product"], "ESA WorldCover")
            self.assertTrue(record["reference_year_differs_from_event"])
            with rasterio.open(output) as image:
                self.assertEqual(image.crs.to_epsg(), 4326)
                self.assertTrue(np.all(image.read(1) == 50))
                self.assertEqual(image.tags()["reference_year"], "2020")

    def test_missing_tiles_and_errors_are_distinct(self):
        response = Mock(status_code=404)
        with patch.object(requests.Session, "head", return_value=response):
            record = worldcover.download_worldcover("2020-01-01", [1, 1, 2, 2], "unused.tif", 2, 2)
        self.assertEqual(record["status"], "no_data")
        with patch.object(worldcover, "worldcover_pixels", side_effect=requests.Timeout()):
            record = worldcover.download_worldcover("2020-01-01", [1, 1, 2, 2], "unused.tif", 2, 2)
        self.assertEqual(record["status"], "error")


if __name__ == "__main__":
    unittest.main()
