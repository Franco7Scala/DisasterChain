import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import rasterio
from PIL import Image
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support import multimodal_satellite_engine as engine
from support import satellite_rendering as rendering


# Produces small georeferenced rasters in memory for offline Process API tests.
def raster_bytes(pixels, bounds):
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff", width=pixels.shape[2], height=pixels.shape[1], count=pixels.shape[0],
            dtype=pixels.dtype, crs="EPSG:4326",
            transform=from_bounds(*bounds, pixels.shape[2], pixels.shape[1]),
        ) as image:
            image.write(pixels)
        return memory.read()


# Packages mock TIFF responses using the same identifiers as Sentinel Hub.
def response_tar(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in files:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class RenderingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bbox = [12, 42, 12.0006, 42.0004]
        self.scene = {"properties": {"datetime": "2020-01-15T12:00:00Z"}}
        self.config = engine.MultimodalSatelliteConfig()
        grid = patch.object(engine, "image_dimensions_for_resolution", return_value=(3, 2))
        grid.start()
        self.addCleanup(grid.stop)
        self.mask = np.ones((1, 2, 3), dtype="uint8")
        self.mask[0, 0, 2] = 0
        self.s2 = np.full((10, 2, 3), 0.125, dtype="float32")
        self.s2[1] = 0.25
        self.s2[2] = 0.5
        self.s2[6] = 0.0625
        self.s2[9] = 0.75
        self.s2[:, 1, 0] = 0
        self.s1 = np.full((2, 2, 3), 0.01, dtype="float32")
        self.s1[1] = 0.1
        self.client = Mock()
        self.client.process_bytes.side_effect = self.respond

    def respond(self, payload, accept, context):
        self.assertEqual(accept, "application/tar")
        responses = payload["output"]["responses"]
        self.assertEqual([item["identifier"] for item in responses], ["default", "data_mask"])
        self.assertTrue(all(item["format"]["type"] == "image/tiff" for item in responses))
        self.assertIn('sampleType: "FLOAT32"', payload["evalscript"])
        self.assertIn('id: "data_mask"', payload["evalscript"])
        self.assertIn('sampleType: "UINT8"', payload["evalscript"])
        self.assertNotIn("S2_RAW_BAND", payload["evalscript"])
        collection = payload["input"]["data"][0]["type"]
        pixels = self.s2 if collection == "sentinel-2-l2a" else self.s1
        return response_tar([
            ("default.tif", raster_bytes(pixels, self.bbox)),
            ("data_mask.tif", raster_bytes(self.mask, self.bbox)),
        ])

    def download_s2(self):
        return engine.download_s2_daily_layers(self.client, self.scene, self.bbox, self.root, self.config)

    def test_s2_one_request_preserves_bands_grid_and_color_order(self):
        outputs = self.download_s2()
        self.client.process_bytes.assert_called_once()
        self.client.process_image.assert_not_called()
        self.assertEqual(set(outputs), {"raw_bands_tif", "data_mask_tif", "true_color_tif", "false_color_tif",
                                        "true_color_preview_png", "false_color_preview_png"})
        with rasterio.open(outputs["raw_bands_tif"]) as raw:
            np.testing.assert_array_equal(raw.read(), self.s2)
            self.assertEqual(raw.count, 10)
            transform = raw.transform
        with rasterio.open(outputs["true_color_tif"]) as image:
            self.assertEqual(image.transform, transform)
            self.assertEqual(image.crs.to_epsg(), 4326)
            self.assertEqual(image.dtypes, ("uint16",) * 3)
            np.testing.assert_array_equal(image.read()[:, 0, 0], [65535, 40959, 20480])
            np.testing.assert_array_equal(image.read()[:, 0, 2], [0, 0, 0])
            self.assertEqual(image.dataset_mask()[0, 2], 0)
            self.assertEqual(image.dataset_mask()[1, 0], 255)
            self.assertEqual(image.tags()["rendering_version"], rendering.RENDERING_VERSION)
        with rasterio.open(outputs["false_color_tif"]) as image:
            np.testing.assert_array_equal(image.read()[:, 0, 0], [65535, 10240, 65535])
        with Image.open(outputs["true_color_preview_png"]) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (3, 2))
            self.assertEqual(image.getpixel((0, 0)), (255, 159, 80))
            self.assertEqual(image.getpixel((2, 0)), (0, 0, 0))

    def test_s1_one_request_preserves_radar_and_db_preview(self):
        self.s1[:, 0, 2] = 0
        self.s1[0, 1, 0] = 0
        self.s1[1, 1, 1] = -1
        self.s1[0, 1, 2] = np.nan
        outputs = engine.download_s1_daily_layers(self.client, self.scene, self.bbox, self.root, self.config)
        self.client.process_bytes.assert_called_once()
        payload = self.client.process_bytes.call_args.args[0]
        self.assertEqual(payload["input"]["data"][0]["processing"]["backCoeff"], "GAMMA0_TERRAIN")
        with rasterio.open(outputs["vv_vh_tif"]) as image:
            np.testing.assert_array_equal(image.read(), self.s1)
        with Image.open(outputs["vv_vh_preview_png"]) as image:
            self.assertEqual(image.getpixel((0, 0)), (51, 153, 255))
            self.assertEqual(image.getpixel((2, 0)), (0, 0, 0))
            self.assertEqual(image.getpixel((0, 1)), (0, 153, 255))
            self.assertEqual(image.getpixel((1, 1)), (51, 0, 255))
            self.assertEqual(image.getpixel((2, 1)), (0, 0, 0))

    def test_missing_preview_is_regenerated_without_another_process_request(self):
        outputs = self.download_s2()
        before = Path(outputs["raw_bands_tif"]).read_bytes()
        Path(outputs["true_color_preview_png"]).unlink()
        self.download_s2()
        self.client.process_bytes.assert_called_once()
        self.assertTrue(Path(outputs["true_color_preview_png"]).exists())
        self.assertEqual(Path(outputs["raw_bands_tif"]).read_bytes(), before)

    def test_render_failure_keeps_completed_raw_download(self):
        with patch.object(engine, "render_s2_layers", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.download_s2()
        self.assertTrue((self.root / "raw_download.json").is_file())
        self.download_s2()
        self.client.process_bytes.assert_called_once()

    def test_changed_acquisition_invalidates_raw_cache(self):
        self.download_s2()
        self.scene["properties"]["datetime"] = "2020-01-15T15:00:00Z"
        self.download_s2()
        self.assertEqual(self.client.process_bytes.call_count, 2)

    def test_missing_mask_invalidates_raw_cache(self):
        self.download_s2()
        (self.root / "data_mask.tif").unlink()
        self.download_s2()
        self.assertEqual(self.client.process_bytes.call_count, 2)
        (self.root / "raw_download.json").write_text("[]", encoding="utf-8")
        self.download_s2()
        self.assertEqual(self.client.process_bytes.call_count, 3)

    def test_previews_can_be_disabled_without_changing_raw_bands(self):
        self.config.include_png_previews = False
        s2 = self.download_s2()
        s1 = engine.download_s1_daily_layers(self.client, self.scene, self.bbox, self.root / "s1", self.config)
        self.assertFalse(any(key.endswith("png") for key in s2 | s1))
        self.assertEqual(self.client.process_bytes.call_count, 2)

    def test_unsafe_or_incomplete_archives_are_not_committed(self):
        raw = raster_bytes(self.s2, self.bbox)
        for files in [
            [("../outside.tif", raw)], [("default.tif", raw)],
            [("default.tif", raw), ("default.tif", raw)],
        ]:
            with self.subTest(files=[name for name, _ in files]):
                self.client.process_bytes.side_effect = None
                self.client.process_bytes.return_value = response_tar(files)
                with self.assertRaises(ValueError):
                    self.download_s2()
                self.assertFalse((self.root / "raw_download.json").exists())
                self.assertFalse((self.root / "raw_bands.tif").exists())
                self.assertFalse(list(self.root.glob("*.part")))
                self.assertFalse((self.root.parent / "outside.tif").exists())

    def test_incompatible_mask_grid_is_rejected(self):
        self.client.process_bytes.side_effect = None
        self.client.process_bytes.return_value = response_tar([
            ("default.tif", raster_bytes(self.s2, self.bbox)),
            ("data_mask.tif", raster_bytes(self.mask, [12, 41, 12.0006, 41.0004])),
        ])
        with self.assertRaisesRegex(ValueError, "different grids"):
            self.download_s2()
        self.assertFalse((self.root / "raw_download.json").exists())

    def test_old_complete_checkpoint_is_reused_without_new_mask(self):
        event = engine.SatelliteEvent(event_id="legacy", start_date="2020-01-15", latitude=42, longitude=12)
        config = engine.MultimodalSatelliteConfig(window_days=0, include_land_cover=False, include_sentinel_3=False)
        legacy_file = self.root / "legacy.tif"
        legacy_file.write_bytes(raster_bytes(self.s2, self.bbox))
        slot = {"status": "available", "available": True, "outputs": {"raw_bands_tif": str(legacy_file)}}
        with patch.object(engine, "require_copernicus_credentials", return_value=("test", "test")), \
                patch.object(engine, "fetch_sensor_day", return_value=slot):
            engine.run_multimodal_satellite_event(event, config, str(self.root))
        with patch.object(engine, "require_copernicus_credentials") as credentials, \
                patch.object(engine, "fetch_sensor_day") as fetch:
            manifest = engine.run_multimodal_satellite_event(event, config, str(self.root))
        self.assertEqual(manifest["status"], "completed")
        credentials.assert_not_called()
        fetch.assert_not_called()
        self.assertNotIn("rendering_version", manifest["days"][0]["sentinel_2"])


if __name__ == "__main__":
    unittest.main()
