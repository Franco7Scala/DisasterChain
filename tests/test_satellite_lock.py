import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fetch_multimodal_satellite_batch as batch
from support import multimodal_satellite_engine as engine
from support.satellite_lock import SatelliteOutputBusyError, satellite_output_lock


class SatelliteLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def child(self, code):
        source_dir = str(Path(__file__).resolve().parents[1] / "src")
        script = (f"import os, sys\nsys.path.insert(0, {source_dir!r})\n"
                  "from support.satellite_lock import satellite_output_lock, SatelliteOutputBusyError\n" + code)
        return subprocess.run([sys.executable, "-c", script, str(self.root)],
                              capture_output=True, text=True, timeout=20)

    def test_second_process_is_refused_and_can_run_after_release(self):
        script = ("try:\n"
                  "    with satellite_output_lock(sys.argv[1]):\n"
                  "        print('acquired')\n"
                  "except SatelliteOutputBusyError:\n"
                  "    sys.exit(5)\n")
        with satellite_output_lock(self.root):
            result = self.child(script)
            self.assertEqual(result.returncode, 5, result.stderr)
        result = self.child(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acquired", result.stdout)

    def test_exception_releases_lock(self):
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with satellite_output_lock(self.root):
                raise RuntimeError("test failure")
        with satellite_output_lock(self.root):
            pass

    def test_abrupt_process_exit_does_not_leave_a_stale_lock(self):
        result = self.child("with satellite_output_lock(sys.argv[1]):\n    os._exit(0)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        with satellite_output_lock(self.root):
            pass

    def test_batch_refuses_second_writer_before_credentials_or_api_calls(self):
        with patch.object(sys, "argv", ["batch", "--all-events", "true", "--output-dir", str(self.root)]):
            args = batch.parse_args()
        with satellite_output_lock(self.root), patch.object(batch, "select_events", return_value=[]), \
                patch.object(batch, "require_copernicus_credentials") as credentials, \
                patch.object(batch, "run_multimodal_satellite_event") as run:
            with self.assertRaisesRegex(SatelliteOutputBusyError, "already in use"):
                batch.run_batch(args)
        credentials.assert_not_called()
        run.assert_not_called()

    def test_event_refuses_second_writer_before_manifest_or_download_changes(self):
        event = engine.SatelliteEvent(event_id="test", start_date="2020-01-15", latitude=42, longitude=12)
        folder = self.root / event.event_id
        with satellite_output_lock(folder), patch.object(engine, "_run_locked_satellite_event") as run:
            with self.assertRaises(SatelliteOutputBusyError):
                engine.run_multimodal_satellite_event(event, engine.MultimodalSatelliteConfig(), str(self.root))
        run.assert_not_called()
        self.assertFalse((folder / "manifest.json").exists())

    def test_dry_run_does_not_create_or_acquire_locks(self):
        output = self.root / "unused"
        with patch.object(sys, "argv", ["batch", "--all-events", "true", "--dry-run", "--output-dir", str(output)]):
            args = batch.parse_args()
        with patch.object(batch, "select_events", return_value=[]), \
                patch.object(batch, "satellite_output_lock") as lock, redirect_stdout(io.StringIO()):
            batch.run_batch(args)
        lock.assert_not_called()
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
