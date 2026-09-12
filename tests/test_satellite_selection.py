import argparse
import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fetch_multimodal_satellite_batch as batch
import prepare_satellite_batch_input as prepare
import run_release_pipeline as release
from support import multimodal_satellite_engine as engine
from support import satellite_selection as selection


def chain_row(event_id="valid", status="parsed", chain=None):
    if chain is None:
        chain = [{"n_event": 1, "type_event": "Flood", "description": "The river overflowed.",
                  "supporting_quote": "The river overflowed."}]
    return {"event_id": event_id, "causal_chain_parse_status": status,
            "causal_chain_json": json.dumps({"causal_chain": chain})}


def write_chains(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["event_id", "causal_chain_parse_status", "causal_chain_json"])
        writer.writeheader()
        writer.writerows(rows)


class CausalEligibilityTests(unittest.TestCase):
    def test_boolean_accepts_only_explicit_true_false(self):
        self.assertFalse(selection.parse_boolean("false"))
        self.assertTrue(selection.parse_boolean("TRUE"))
        with self.assertRaises(argparse.ArgumentTypeError):
            selection.parse_boolean("all")

    def test_retained_steps_are_eligible_even_after_drops(self):
        for status in selection.ACCEPTED_CAUSAL_STATUSES:
            with self.subTest(status=status):
                row = chain_row(status=status)
                row["causal_chain_length"] = "0"
                row["causal_chain_dropped_quote_steps"] = "3"
                self.assertEqual(selection.causal_chain_status(row), "valid")

    def test_empty_skipped_and_invalid_rows_are_excluded(self):
        for status in ("empty_chain", "empty_chain_after_quote_validation", "invalid_json",
                       "skipped_no_accepted_summary", "dry_run", "", None):
            with self.subTest(status=status):
                self.assertEqual(selection.causal_chain_status(chain_row(status=status)), "unaccepted_parse_status")
        self.assertEqual(selection.causal_chain_status(chain_row(chain=[])), "empty_chain")
        row = chain_row()
        row["causal_chain_json"] = "not JSON"
        row["causal_chain_length"] = "3"
        self.assertEqual(selection.causal_chain_status(row), "invalid_json")

    def test_structurally_invalid_steps_are_not_repaired(self):
        step = json.loads(chain_row()["causal_chain_json"])["causal_chain"][0]
        bad_steps = [None, {}, {**step, "n_event": True}, {**step, "n_event": 2},
                     {**step, "n_event": "1"}, {**step, "type_event": ""},
                     {**step, "description": " "}, {**step, "supporting_quote": None}]
        for item in bad_steps:
            with self.subTest(item=item):
                self.assertEqual(selection.causal_chain_status(chain_row(chain=[item])), "invalid_structure")
        for payload in ({"wrong_key": []}, {"causal_chain": "Flood"}, 1, None):
            row = chain_row()
            row["causal_chain_json"] = json.dumps(payload)
            self.assertEqual(selection.causal_chain_status(row), "invalid_structure")

    def test_loader_accepts_list_export_and_id_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "causal.csv"
            row = chain_row()
            chain = json.loads(row["causal_chain_json"])["causal_chain"]
            pd.DataFrame([{"DisNo.": " valid ", "causal_chain_parse_status": "parsed",
                           "causal_chain": json.dumps(chain)}]).to_csv(path, index=False)
            self.assertEqual(selection.read_causal_chain_statuses(str(path)), {"valid": "valid"})

    def test_missing_or_ambiguous_source_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "causal.csv"
            with self.assertRaisesRegex(SystemExit, "not found"):
                selection.read_causal_chain_statuses(str(path))
            pd.DataFrame([{"event_id": "x", "causal_chain_length": 2}]).to_csv(path, index=False)
            with self.assertRaisesRegex(SystemExit, "requires"):
                selection.read_causal_chain_statuses(str(path))
            for rows in ([chain_row(), chain_row()], [chain_row("")]):
                write_chains(path, rows)
                with self.assertRaisesRegex(SystemExit, "Missing or duplicate"):
                    selection.read_causal_chain_statuses(str(path))


class SatelliteSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.geo = self.root / "geo.csv"
        self.events = self.root / "events.csv"
        self.causal = self.root / "causal.csv"
        self.frame = pd.DataFrame({
            "DisNo.": ["empty", "valid", "dropped", "missing_coords", "missing_chain"],
            "Country": ["Italy"] * 5,
            "Disaster Type": ["Flood", "Wildfire", "Road", "Flood", "Storm"],
            "start_date": ["2020-01-15"] * 5,
            "latitude": [42, 43, 44, None, 45], "longitude": [12] * 5,
        })
        self.frame.to_csv(self.geo, index=False)
        prepare.build_satellite_input(self.frame, all_events=True).to_csv(self.events, index=False)
        write_chains(self.causal, [chain_row("empty", "empty_chain", []), chain_row(),
                                  chain_row("dropped", "parsed_with_dropped_unsupported_quotes"),
                                  chain_row("missing_coords")])

    def batch_args(self, *extra):
        argv = ["batch", "--events-csv", str(self.events), "--causal-csv", str(self.causal), *extra]
        with patch.object(sys, "argv", argv):
            return batch.parse_args()

    def test_default_audit_intersects_chains_and_coordinates(self):
        selected, audit = prepare.satellite_input_with_audit(
            self.frame, disaster_type="", min_start_date="2014-04-03",
            causal_chain_statuses=selection.read_causal_chain_statuses(str(self.causal)),
        )
        self.assertEqual(selected.emdat_disaster_id.tolist(), ["valid", "dropped"])
        self.assertEqual(audit.selection_status.tolist(), ["no_valid_causal_chain", "selected", "selected",
                                                         "missing_coordinates", "no_valid_causal_chain"])
        self.assertEqual(audit.iloc[-1].causal_chain_status, "missing_causal_chain")
        self.assertFalse(audit.satellite_all_events.any())
        with self.assertRaisesRegex(SystemExit, "eligibility is required"):
            prepare.build_satellite_input(self.frame)

    def test_preparation_writes_filtered_csv_and_audit_without_touching_downloads(self):
        checkpoint = self.root / "manifest.json"
        checkpoint.write_text("unchanged", encoding="utf-8")
        argv = ["prepare", "--input-csv", str(self.geo), "--output-csv", str(self.events),
                "--causal-csv", str(self.causal)]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            prepare.main()
        self.assertEqual(pd.read_csv(self.events).emdat_disaster_id.tolist(), ["valid", "dropped"])
        audit = pd.read_csv(self.root / "events_selection_audit.csv")
        self.assertEqual(len(audit), 5)
        self.assertTrue(audit.causal_chain_source_csv.eq(str(self.causal.resolve())).all())
        self.assertEqual(checkpoint.read_text(), "unchanged")

    def test_all_mode_ignores_missing_causal_file_and_rebuilds_full_input(self):
        self.causal.unlink()
        argv = ["prepare", "--input-csv", str(self.geo), "--output-csv", str(self.events),
                "--causal-csv", str(self.causal), "--all-events", "true"]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            prepare.main()
        self.assertEqual(len(pd.read_csv(self.events)), 4)
        self.assertEqual(len(batch.select_events(self.batch_args("--all-events", "true", "--limit", "0"))), 4)

    def test_missing_source_preserves_previous_input_and_makes_no_requests(self):
        original = self.events.read_bytes()
        self.causal.unlink()
        with patch.object(sys, "argv", ["prepare", "--input-csv", str(self.geo), "--output-csv", str(self.events),
                                        "--causal-csv", str(self.causal)]):
            with self.assertRaisesRegex(SystemExit, "not found"):
                prepare.main()
        self.assertEqual(self.events.read_bytes(), original)
        with patch.object(batch, "require_copernicus_credentials") as credentials, \
                patch.object(batch, "run_multimodal_satellite_event") as run:
            with self.assertRaisesRegex(SystemExit, "not found"):
                batch.run_batch(self.batch_args("--limit", "0"))
        credentials.assert_not_called()
        run.assert_not_called()

    def test_stale_all_event_input_is_filtered_before_limit(self):
        events = batch.select_events(self.batch_args("--limit", "1"))
        self.assertEqual([event.event_id for event in events], ["valid"])
        self.assertEqual(len(batch.select_events(self.batch_args("--limit", "0"))), 2)
        events = batch.select_events(self.batch_args("--disaster-type", "Road", "--limit", "0"))
        self.assertEqual([event.event_id for event in events], ["dropped"])

    def test_manual_ids_cannot_bypass_default_policy(self):
        with self.assertRaisesRegex(SystemExit, "without a valid causal chain"):
            batch.select_events(self.batch_args("--event-id", "empty"))
        selected = batch.select_events(self.batch_args("--event-id", "empty", "--all-events", "true"))
        self.assertEqual(selected[0].event_id, "empty")

    def test_no_matching_chains_fail_before_credentials_or_downloads(self):
        write_chains(self.causal, [chain_row("empty", "empty_chain", [])])
        with patch.object(batch, "require_copernicus_credentials") as credentials, \
                patch.object(batch, "run_multimodal_satellite_event") as run:
            with self.assertRaisesRegex(SystemExit, "No valid events"):
                batch.run_batch(self.batch_args("--limit", "0"))
        credentials.assert_not_called()
        run.assert_not_called()

    def test_default_dry_run_makes_no_api_calls(self):
        with patch.object(batch, "require_copernicus_credentials") as credentials, \
                patch.object(batch, "run_multimodal_satellite_event") as run, redirect_stdout(io.StringIO()) as output:
            batch.run_batch(self.batch_args("--limit", "0", "--dry-run"))
        credentials.assert_not_called()
        run.assert_not_called()
        self.assertIn("Dry run selected 2 event(s)", output.getvalue())

    def test_filtered_run_reuses_legacy_completed_checkpoint(self):
        args = self.batch_args("--limit", "1", "--output-dir", str(self.root),
                               "--summary-csv", str(self.root / "summary.csv"))
        event = batch.select_events(args)[0]
        folder = self.root / event.event_id
        folder.mkdir()
        image = folder / "existing.tif"
        image.write_bytes(b"existing satellite data")
        days = [{sensor: {"status": "no_scene"} for sensor in ("sentinel_2", "sentinel_1", "sentinel_3_slstr")}
                for _ in range(21)]
        days[0]["sentinel_2"] = {"status": "available", "available": True, "outputs": {"raw_tif": str(image)}}
        manifest = {"schema_version": engine.MULTIMODAL_SCHEMA_VERSION, "event": asdict(event),
                    "config": asdict(batch.build_config(args)), "status": "completed", "days": days,
                    "temporal_window": {"days": 21}, "land_cover": {"status": "no_data"}}
        path = folder / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        original = path.read_bytes()
        with patch.object(batch, "require_copernicus_credentials"), \
                patch.object(batch, "run_multimodal_satellite_event") as run, redirect_stdout(io.StringIO()):
            batch.run_batch(args)
        run.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(pd.read_csv(self.root / "summary.csv").iloc[0]["status"], "skipped_existing")


class ReleaseSelectionTests(unittest.TestCase):
    def test_main_passes_boolean_and_custom_causal_path_to_both_steps(self):
        for options, expected in (([], False), (["--satellite-all-events", "false"], False),
                                  (["--satellite-all-events", "true"], True)):
            with self.subTest(options=options), patch.object(sys, "argv", ["release", *options,
                                                                          "--normalized-causal-csv", "custom.csv"]):
                args = release.parse_args()
            self.assertEqual(args.satellite_all_events, expected)
            steps = {step.name: step for step in release.build_steps(args)}
            for name in ("satellite-input", "satellite-general"):
                command = steps[name].commands[0]
                self.assertEqual(command[command.index("--all-events") + 1], str(expected).lower())
                self.assertEqual(command[command.index("--causal-csv") + 1], "custom.csv")
                self.assertEqual("custom.csv" in steps[name].required_inputs, not expected)
            ordered = release.expand_requested_steps(["all"])
            self.assertLess(ordered.index("causal-type-normalization"), ordered.index("satellite-input"))

    def test_invalid_boolean_does_not_silently_enable_all_events(self):
        for parser, flag in ((release.parse_args, "--satellite-all-events"), (prepare.parse_args, "--all-events"),
                             (batch.parse_args, "--all-events")):
            with patch.object(sys, "argv", ["script", flag, "yes"]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser()


if __name__ == "__main__":
    unittest.main()
