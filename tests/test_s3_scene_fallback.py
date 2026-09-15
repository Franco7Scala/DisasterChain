import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support import multimodal_satellite_engine as engine
from support import satellite_engine as shared


SOURCE_ERROR = {
    "error": {
        "status": 500,
        "code": "RENDERER_EXCEPTION",
        "message": "Illegal request to creo://eodata/Sentinel-3/SLSTR/product/S9_BT_in.nc. HTTP Status: '404'",
    }
}


def scene(hour, cloud=0, day="2025-10-30", suffix=""):
    return {"id": f"scene-{hour}-{suffix}", "properties": {
        "datetime": f"{day}T{hour:02d}:00:00Z", "eo:cloud_cover": cloud,
    }}


def missing_source():
    return shared.SentinelHubRequestError("SLSTR source missing", 500, response_body=SOURCE_ERROR)


class SourceErrorTests(unittest.TestCase):
    def test_structured_body_survives_display_truncation(self):
        details = {**SOURCE_ERROR, "padding": "x" * 10000}
        response = Mock(ok=False, status_code=500, reason="Internal Server Error", text=json.dumps(details))
        with self.assertRaises(shared.SentinelHubRequestError) as caught:
            shared.SentinelHubClient._raise_api_error(response, "Process request")
        self.assertEqual(caught.exception.response_body, details)
        self.assertTrue(engine.missing_s3_source_file(caught.exception))
        self.assertLess(len(str(caught.exception)), len(response.text))

    def test_unstructured_errors_are_not_source_file_errors(self):
        for body in ("<html>Server error</html>", "[]", "null"):
            with self.subTest(body=body):
                response = Mock(ok=False, status_code=500, reason="Server error", text=body)
                with self.assertRaises(shared.SentinelHubRequestError) as caught:
                    shared.SentinelHubClient._raise_api_error(response, "Process request")
                self.assertFalse(engine.missing_s3_source_file(caught.exception))

    def test_detection_rejects_other_statuses_codes_and_collections(self):
        cases = [
            shared.SentinelHubRequestError("missing", 404, response_body=SOURCE_ERROR),
            shared.SentinelHubRequestError("missing", 500, response_body={"error": "invalid"}),
            shared.SentinelHubRequestError("missing", 500, response_body={"error": {
                **SOURCE_ERROR["error"], "code": "OTHER_ERROR",
            }}),
            shared.SentinelHubRequestError("missing", 500, response_body={"error": {
                **SOURCE_ERROR["error"],
                "message": SOURCE_ERROR["error"]["message"].replace("Sentinel-3/SLSTR", "Sentinel-2"),
            }}),
        ]
        for error in cases:
            self.assertFalse(engine.missing_s3_source_file(error))


class SceneFallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.client = Mock()
        self.client.catalog_search.return_value = [scene(19), scene(7, cloud=10)]
        self.config = engine.MultimodalSatelliteConfig(window_days=0, include_land_cover=False)
        self.bbox = [40, 8, 40.2, 8.2]
        self.image = self.root / "thermal_bands.tif"
        self.image.write_bytes(b"synthetic thermal image")
        self.outputs = {"thermal_bands_tif": str(self.image)}

    def fetch(self):
        return engine.fetch_sensor_day(self.client, self.bbox, "2025-10-30", "sentinel_3_slstr",
                                       self.root, self.config)

    def test_missing_source_uses_same_day_alternative_and_keeps_audit(self):
        first, second = self.client.catalog_search.return_value
        with patch.object(engine, "download_s3_daily_layers", side_effect=[missing_source(), self.outputs]) as download:
            slot = self.fetch()
        self.assertEqual([call.args[1] for call in download.call_args_list], [first, second])
        for call in download.call_args_list:
            self.assertEqual(call.args[2], self.bbox)
            self.assertIs(call.args[4], self.config)
        self.assertEqual(slot["scene"], engine.scene_record(second))
        self.assertEqual([item["status"] for item in slot["scene_attempts"]], ["error", "available"])
        self.assertTrue(slot["scene_attempts"][0]["missing_source_file"])
        self.assertTrue(engine.slot_complete(slot))

    def test_successful_primary_acquisition_needs_no_alternative(self):
        with patch.object(engine, "download_s3_daily_layers", return_value=self.outputs) as download:
            slot = self.fetch()
        download.assert_called_once()
        self.assertEqual(len(slot["scene_attempts"]), 1)
        self.assertTrue(engine.slot_complete(slot))

    def test_no_candidates_means_no_scene_without_process_calls(self):
        self.client.catalog_search.return_value = []
        with patch.object(engine, "download_s3_daily_layers") as download:
            slot = self.fetch()
        download.assert_not_called()
        self.assertEqual(slot["status"], "no_scene")
        self.assertTrue(engine.slot_complete(slot))

    def test_attempts_are_bounded_and_skip_duplicate_windows_and_other_days(self):
        overlap = scene(19, suffix="overlap")
        overlap["properties"]["datetime"] = "2025-10-30T19:00:01Z"
        self.client.catalog_search.return_value = [
            scene(19), scene(19, suffix="duplicate"), overlap,
            scene(7, cloud=10), scene(11, cloud=20), scene(15, cloud=30),
            scene(2, day="2025-10-31"),
        ]
        with patch.object(engine, "download_s3_daily_layers", side_effect=missing_source()) as download:
            slot = self.fetch()
        self.assertEqual(download.call_count, 3)
        self.assertEqual([call.args[1]["properties"]["datetime"] for call in download.call_args_list], [
            "2025-10-30T19:00:00Z", "2025-10-30T07:00:00Z", "2025-10-30T11:00:00Z",
        ])
        self.assertEqual(slot["status"], "error")
        self.assertFalse(slot["available"])
        self.assertEqual(slot["outputs"], {})
        self.assertEqual(len(slot["scene_attempts"]), 3)
        self.assertFalse(engine.slot_complete(slot))

    def test_single_broken_candidate_stays_error_not_no_scene(self):
        self.client.catalog_search.return_value = [scene(19)]
        with patch.object(engine, "download_s3_daily_layers", side_effect=missing_source()) as download:
            slot = self.fetch()
        download.assert_called_once()
        self.assertEqual(slot["status"], "error")
        self.assertFalse(engine.slot_complete(slot))

    def test_generic_server_error_does_not_try_other_scenes(self):
        with patch.object(engine, "download_s3_daily_layers",
                          side_effect=shared.SentinelHubRequestError("temporary", 500)) as download:
            slot = self.fetch()
        download.assert_called_once()
        self.assertEqual(slot["status"], "error")
        self.assertFalse(slot["scene_attempts"][0]["missing_source_file"])

    def test_authentication_and_quota_errors_still_raise(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), patch.object(engine, "download_s3_daily_layers",
                    side_effect=shared.SentinelHubRequestError("stop", status)) as download:
                with self.assertRaises(shared.SentinelHubRequestError):
                    self.fetch()
                download.assert_called_once()

    def test_partial_event_resume_only_requests_its_failed_sensor_and_is_reusable(self):
        event = shared.SatelliteEvent("2025-0965-ETH", 8, 40, "2025-10-30")
        available = {"status": "available", "available": True, "outputs": self.outputs}
        no_scene = {**engine.empty_sensor_slot(), "status": "no_scene"}
        with patch.object(engine, "require_copernicus_credentials", return_value=("test", "test")), \
                patch.object(engine, "DailyCatalogClient", return_value=self.client):
            with patch.object(engine, "fetch_sensor_day", side_effect=[available, no_scene, missing_source()]):
                manifest = engine.run_multimodal_satellite_event(event, self.config, str(self.root))
            self.assertEqual(manifest["status"], "partial")
            with patch.object(engine, "download_s3_daily_layers", side_effect=[missing_source(), self.outputs]) as download, \
                    patch.object(engine, "download_s2_daily_layers") as s2, \
                    patch.object(engine, "download_s1_daily_layers") as s1:
                manifest = engine.run_multimodal_satellite_event(event, self.config, str(self.root))
            self.assertEqual(download.call_count, 2)
            s2.assert_not_called()
            s1.assert_not_called()
            self.assertEqual(manifest["status"], "completed")
            saved = json.loads(Path(manifest["manifest_path"]).read_text())
            self.assertEqual(len(saved["days"][0]["sentinel_3_slstr"]["scene_attempts"]), 2)
            with patch.object(engine, "fetch_sensor_day") as fetch:
                engine.run_multimodal_satellite_event(event, self.config, str(self.root))
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
