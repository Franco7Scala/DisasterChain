from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_START_DATE = "2014-04-03"
DEFAULT_BASE_CSV = "results/disasters_per_satellite.csv"
DEFAULT_EMDAT_FILE = "data/public_emdat_dal_2000.xlsx"
DEFAULT_GDIS_FILE = "data/pend-gdis-1960-2018-disasterlocations.csv"
DEFAULT_LLM_LOCATION_CSV = (
    "results/recent_emdat_geocoding/llm_location_cleaning_new_prompt_all_70b_from_2014-04-03.csv"
)
DEFAULT_LLM_NOMINATIM_QUERIES_CSV = (
    "results/recent_emdat_geocoding/llm_location_nominatim_queries_new_prompt_all_70b_from_2014-04-03.csv"
)
DEFAULT_LLM_NOMINATIM_RESULTS_CSV = (
    "results/recent_emdat_geocoding/llm_location_nominatim_results_new_prompt_all_70b_from_2014-04-03.csv"
)
DEFAULT_FINAL_GEOCODING_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2.csv"
)
DEFAULT_FINAL_POSITIONS_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2_positions.csv"
)
DEFAULT_FINAL_POSITION_SUMMARY_CSV = (
    "results/recent_emdat_geocoding/emdat_2014_final_llm70b_review_resolved_v2_position_summary.csv"
)
DEFAULT_REVIEW_AUDIT_CSV = (
    "results/recent_emdat_geocoding/llm70b_nominatim_review_resolution_audit_v2_from_2014-04-03.csv"
)
DEFAULT_REVIEW_SUMMARY_CSV = (
    "results/recent_emdat_geocoding/llm70b_nominatim_review_resolution_summary_v2_from_2014-04-03.csv"
)
DEFAULT_WEATHER_JSON = "results/weather/weather_2014_plus.json"
DEFAULT_WEATHER_PROGRESS_CSV = "results/weather/weather_2014_plus_progress.csv"
DEFAULT_ADMIN_UNITS_CSV = "results/recent_emdat_geocoding/admin_units.csv"
DEFAULT_ADMIN_UNIT_BBOXES_CSV = "results/recent_emdat_geocoding/admin_unit_bboxes.csv"
DEFAULT_ADM2_AOIS_CSV = "results/recent_emdat_geocoding/event_aois_adm2.csv"
DEFAULT_NEWS_JSON = (
    "results/news_reasoning/final_environmental_causal_dataset_2014_plus_news.json"
)
DEFAULT_NEWS_PROGRESS_CSV = (
    "results/news_reasoning/final_environmental_causal_dataset_2014_plus_news_progress.csv"
)
DEFAULT_SUMMARY_JSONL = (
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5.jsonl"
)
DEFAULT_SUMMARY_CSV = (
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5.csv"
)
DEFAULT_SUMMARY_COVERAGE_CSV = (
    "results/news_reasoning/event_news_summaries_coverage_2014_plus_llm70b_v5.csv"
)
DEFAULT_VALIDATED_SUMMARY_JSONL = (
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5_validated.jsonl"
)
DEFAULT_VALIDATED_SUMMARY_CSV = (
    "results/news_reasoning/event_news_summaries_2014_plus_llm70b_v5_validated.csv"
)
DEFAULT_VALIDATED_SUMMARY_COVERAGE_CSV = (
    "results/news_reasoning/event_news_summaries_coverage_2014_plus_llm70b_v5_validated.csv"
)
DEFAULT_CAUSAL_JSONL = (
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus.jsonl"
)
DEFAULT_CAUSAL_CSV = "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus.csv"
DEFAULT_CAUSAL_COVERAGE_CSV = (
    "results/news_reasoning/event_causal_chains_coverage_qwen72b_v8_2014_plus.csv"
)
DEFAULT_NORMALIZED_CAUSAL_JSONL = (
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus_type_normalized.jsonl"
)
DEFAULT_NORMALIZED_CAUSAL_CSV = (
    "results/news_reasoning/event_causal_chains_qwen72b_v8_2014_plus_type_normalized.csv"
)
DEFAULT_TYPE_MAP_CSV = (
    "results/news_reasoning/event_causal_chain_type_normalization_map_qwen72b_v8_2014_plus.csv"
)
DEFAULT_TYPE_SUMMARY_CSV = (
    "results/news_reasoning/event_causal_chain_type_normalization_summary_qwen72b_v8_2014_plus.csv"
)
DEFAULT_SATELLITE_INPUT_CSV = (
    "results/multimodal_satellite_2014_plus/events.csv"
)
DEFAULT_SATELLITE_SUMMARY_CSV = (
    "results/multimodal_satellite_2014_plus/batch_summary.csv"
)
DEFAULT_SATELLITE_RANKING_CSV = ""
DEFAULT_FINAL_COMPLETE_CSV = "results/final_environmental_causal_dataset_2014_plus.csv"
DEFAULT_FINAL_COMPLETE_SUMMARY_CSV = (
    "results/final_environmental_causal_dataset_2014_plus_summary.csv"
)
DEFAULT_LOCATION_MODEL = "meta-llama/Llama-3.1-70B-Instruct"
DEFAULT_SUMMARY_MODEL = "meta-llama/Llama-3.1-70B-Instruct"
DEFAULT_CAUSAL_MODEL = "Qwen/Qwen2.5-72B-Instruct"


@dataclass(frozen=True)
class PipelineStep:
    name: str
    description: str
    commands: List[List[str]]
    required_inputs: List[str]
    expected_outputs: List[str]
    note: str = ""


# Returns the interpreter used for all child Python commands.
def python_executable(args: argparse.Namespace) -> str:
    return args.python or sys.executable


# Resolves a project-relative path without breaking absolute paths.
def project_path(path: str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else PROJECT_ROOT / value


# Adds the selected quantization flag to an LLM command.
def add_quantization(command: List[str], args: argparse.Namespace) -> List[str]:
    if args.llm_quantization == "4bit":
        return command + ["--load-in-4bit"]
    if args.llm_quantization == "8bit":
        return command + ["--load-in-8bit"]
    return command


# Adds --force to commands that support resume-safe output regeneration.
def add_force(command: List[str], args: argparse.Namespace) -> List[str]:
    return command + ["--force"] if args.force else command


# Builds the ordered release pipeline from the project scripts.
def build_steps(args: argparse.Namespace) -> List[PipelineStep]:
    py = python_executable(args)
    final_csv = args.final_geocoding_csv
    steps: List[PipelineStep] = []

    steps.append(
        PipelineStep(
            name="base-merge",
            description="Merge raw EM-DAT and GDIS into the base event table.",
            commands=[
                [
                    py,
                    "src/build_base_dataset.py",
                    "--emdat-file",
                    args.emdat_file,
                    "--gdis-file",
                    args.gdis_file,
                    "--output-csv",
                    args.base_csv,
                ]
            ],
            required_inputs=[args.emdat_file, args.gdis_file],
            expected_outputs=[args.base_csv],
        )
    )

    steps.append(
        PipelineStep(
            name="geocoding-analyze",
            description="Analyze EM-DAT 2014+ rows and extract admin-unit candidates.",
            commands=[
                [
                    py,
                    "src/analyze_recent_emdat_geocoding.py",
                    "--emdat-path",
                    args.emdat_file,
                    "--start-date",
                    args.start_date,
                    "--candidates-csv",
                    args.geocoding_candidates_csv,
                    "--summary-csv",
                    args.geocoding_summary_csv,
                    "--admin-units-csv",
                    args.admin_units_csv,
                ]
            ],
            required_inputs=[args.emdat_file],
            expected_outputs=[
                args.geocoding_candidates_csv,
                args.geocoding_summary_csv,
                args.admin_units_csv,
            ],
        )
    )

    steps.append(
        PipelineStep(
            name="gadm-download",
            description="Download the GADM files needed for administrative fallback.",
            commands=[
                [
                    py,
                    "src/download_gadm_files.py",
                    "--admin-units-csv",
                    args.admin_units_csv,
                    "--gadm-dir",
                    args.gadm_dir,
                    "--plan-csv",
                    args.gadm_plan_csv,
                    "--top-countries",
                    str(args.gadm_top_countries),
                    "--sleep-seconds",
                    str(args.gadm_sleep_seconds),
                    "--download",
                ]
            ],
            required_inputs=[args.admin_units_csv],
            expected_outputs=[args.gadm_plan_csv],
            note="Use --gadm-top-countries 0 to include all countries from the plan.",
        )
    )

    steps.append(
        PipelineStep(
            name="gadm-bboxes",
            description="Match EM-DAT admin units to local GADM geometries.",
            commands=[
                [
                    py,
                    "src/build_admin_unit_bboxes.py",
                    "--admin-units-csv",
                    args.admin_units_csv,
                    "--gadm-dir",
                    args.gadm_dir,
                    "--output-csv",
                    args.admin_unit_bboxes_csv,
                ]
            ],
            required_inputs=[args.admin_units_csv],
            expected_outputs=[args.admin_unit_bboxes_csv],
        )
    )

    steps.append(
        PipelineStep(
            name="adm2-aois",
            description="Build ADM2 AOIs used as ADM/GADM coordinate fallback.",
            commands=[
                [
                    py,
                    "src/build_event_bboxes.py",
                    "--admin-unit-bboxes-csv",
                    args.admin_unit_bboxes_csv,
                    "--output-csv",
                    args.adm2_aois_csv,
                    "--separate-units",
                    "--unit-level",
                    "2",
                ]
            ],
            required_inputs=[args.admin_unit_bboxes_csv],
            expected_outputs=[args.adm2_aois_csv],
        )
    )

    location_command = [
        py,
        "src/clean_emdat_locations_with_llm.py",
        "--input-file",
        args.emdat_file,
        "--start-date",
        args.start_date,
        "--limit",
        str(args.location_limit),
        "--max-new-tokens",
        str(args.location_max_new_tokens),
        "--model-name",
        args.location_model_name,
        "--output-csv",
        args.llm_location_csv,
    ]
    steps.append(
        PipelineStep(
            name="llm-location",
            description="Extract canonical geocoding strings from EM-DAT locations.",
            commands=[add_quantization(location_command, args)],
            required_inputs=[args.emdat_file],
            expected_outputs=[args.llm_location_csv],
            note="Requires HuggingFace access to the configured location model.",
        )
    )

    steps.append(
        PipelineStep(
            name="llm-nominatim-queries",
            description="Build unique country-constrained Nominatim queries.",
            commands=[
                [
                    py,
                    "src/prepare_llm_location_nominatim_queries.py",
                    "--llm-csv",
                    args.llm_location_csv,
                    "--emdat-file",
                    args.emdat_file,
                    "--start-date",
                    args.start_date,
                    "--output-csv",
                    args.llm_nominatim_queries_csv,
                    "--summary-csv",
                    args.llm_nominatim_queries_summary_csv,
                ]
            ],
            required_inputs=[args.llm_location_csv, args.emdat_file],
            expected_outputs=[
                args.llm_nominatim_queries_csv,
                args.llm_nominatim_queries_summary_csv,
            ],
        )
    )

    steps.append(
        PipelineStep(
            name="llm-nominatim-geocode",
            description="Geocode the LLM-generated location strings with Nominatim.",
            commands=[
                [
                    py,
                    "src/geocode_text_queries.py",
                    "--queries-csv",
                    args.llm_nominatim_queries_csv,
                    "--results-csv",
                    args.llm_nominatim_results_csv,
                    "--limit",
                    str(args.nominatim_limit),
                    "--sleep-seconds",
                    str(args.nominatim_sleep_seconds),
                    "--variant-sleep-seconds",
                    str(args.nominatim_variant_sleep_seconds),
                ]
            ],
            required_inputs=[args.llm_nominatim_queries_csv],
            expected_outputs=[args.llm_nominatim_results_csv],
            note="Uses the public Nominatim endpoint and should keep a polite delay.",
        )
    )

    steps.append(
        PipelineStep(
            name="geocoding-final",
            description="Resolve final coordinates from EM-DAT, ADM/GADM, and LLM+Nominatim.",
            commands=[
                [
                    py,
                    "src/resolve_final_geocoding.py",
                    "--emdat-file",
                    args.emdat_file,
                    "--start-date",
                    args.start_date,
                    "--llm-csv",
                    args.llm_location_csv,
                    "--nominatim-results-csv",
                    args.llm_nominatim_results_csv,
                    "--adm2-aois-csv",
                    args.adm2_aois_csv,
                    "--output-csv",
                    final_csv,
                    "--positions-csv",
                    args.final_positions_csv,
                    "--summary-csv",
                    args.final_position_summary_csv,
                    "--review-audit-csv",
                    args.review_audit_csv,
                    "--review-summary-csv",
                    args.review_summary_csv,
                ]
            ],
            required_inputs=[
                args.emdat_file,
                args.llm_location_csv,
                args.llm_nominatim_results_csv,
            ],
            expected_outputs=[
                final_csv,
                args.final_positions_csv,
                args.final_position_summary_csv,
                args.review_audit_csv,
                args.review_summary_csv,
            ],
        )
    )

    steps.append(
        PipelineStep(
            name="weather",
            description="Fetch weather data for final geocoded 2014+ events.",
            commands=[
                [
                    py,
                    "src/fetch_weather_batch.py",
                    "--input-csv",
                    final_csv,
                    "--output-json",
                    args.weather_json,
                    "--progress-csv",
                    args.weather_progress_csv,
                    "--limit",
                    str(args.weather_limit),
                    "--sleep-seconds",
                    str(args.weather_sleep_seconds),
                    "--max-retries",
                    str(args.weather_max_retries),
                ]
            ],
            required_inputs=[final_csv],
            expected_outputs=[args.weather_json, args.weather_progress_csv],
            note="Uses Open-Meteo first and NASA POWER as fallback.",
        )
    )

    steps.append(
        PipelineStep(
            name="news",
            description="Collect news articles for the final geocoded 2014+ events.",
            commands=[
                [
                    py,
                    "src/build_news_dataset_from_geocoded_events.py",
                    "--input-csv",
                    final_csv,
                    "--output-json",
                    args.news_json,
                    "--progress-csv",
                    args.news_progress_csv,
                    "--limit",
                    str(args.news_limit),
                    "--sleep-seconds",
                    str(args.news_sleep_seconds),
                ]
            ],
            required_inputs=[final_csv],
            expected_outputs=[args.news_json, args.news_progress_csv],
        )
    )

    summary_command = [
        py,
        "src/generate_event_news_summaries.py",
        "--input-json",
        args.news_json,
        "--event-csv",
        final_csv,
        "--output-jsonl",
        args.summary_jsonl,
        "--output-csv",
        args.summary_csv,
        "--coverage-csv",
        args.summary_coverage_csv,
        "--limit",
        str(args.summary_limit),
        "--summary-max-new-tokens",
        str(args.summary_max_new_tokens),
        "--model-name",
        args.summary_model_name,
    ]
    steps.append(
        PipelineStep(
            name="summary",
            description="Generate LLM event summaries from metadata and relevant news.",
            commands=[add_force(add_quantization(summary_command, args), args)],
            required_inputs=[args.news_json, final_csv],
            expected_outputs=[args.summary_jsonl, args.summary_csv, args.summary_coverage_csv],
            note="Requires HuggingFace access to the configured summary model.",
        )
    )

    steps.append(
        PipelineStep(
            name="summary-validation",
            description="Validate generated summaries and mark unsupported ones as insufficient.",
            commands=[
                [
                    py,
                    "src/validate_event_news_summaries.py",
                    "--input-csv",
                    args.summary_csv,
                    "--output-csv",
                    args.validated_summary_csv,
                    "--input-jsonl",
                    args.summary_jsonl,
                    "--output-jsonl",
                    args.validated_summary_jsonl,
                    "--input-coverage-csv",
                    args.summary_coverage_csv,
                    "--output-coverage-csv",
                    args.validated_summary_coverage_csv,
                ]
            ],
            required_inputs=[args.summary_csv],
            expected_outputs=[
                args.validated_summary_csv,
                args.validated_summary_jsonl,
                args.validated_summary_coverage_csv,
            ],
        )
    )

    causal_command = [
        py,
        "src/generate_event_causal_chains.py",
        "--input-json",
        args.news_json,
        "--event-csv",
        final_csv,
        "--summary-csv",
        args.validated_summary_csv,
        "--output-jsonl",
        args.causal_jsonl,
        "--output-csv",
        args.causal_csv,
        "--coverage-csv",
        args.causal_coverage_csv,
        "--limit",
        str(args.causal_limit),
        "--causal-chain-max-new-tokens",
        str(args.causal_chain_max_new_tokens),
        "--model-name",
        args.causal_model_name,
        "--require-accepted-summary",
        "--accepted-summary-only",
    ]
    steps.append(
        PipelineStep(
            name="causal-chain",
            description="Extract causal chains with Qwen from events with accepted summaries.",
            commands=[add_force(add_quantization(causal_command, args), args)],
            required_inputs=[args.news_json, final_csv, args.validated_summary_csv],
            expected_outputs=[args.causal_jsonl, args.causal_csv, args.causal_coverage_csv],
            note="Requires HuggingFace access to the configured causal-chain model.",
        )
    )

    steps.append(
        PipelineStep(
            name="causal-type-normalization",
            description="Normalize causal-chain type_event labels without calling the LLM again.",
            commands=[
                [
                    py,
                    "src/normalize_event_causal_chain_types.py",
                    "--input-csv",
                    args.causal_csv,
                    "--output-csv",
                    args.normalized_causal_csv,
                    "--input-jsonl",
                    args.causal_jsonl,
                    "--output-jsonl",
                    args.normalized_causal_jsonl,
                    "--type-map-csv",
                    args.type_map_csv,
                    "--summary-csv",
                    args.type_summary_csv,
                ]
            ],
            required_inputs=[args.causal_csv],
            expected_outputs=[
                args.normalized_causal_csv,
                args.normalized_causal_jsonl,
                args.type_map_csv,
                args.type_summary_csv,
            ],
        )
    )

    steps.append(
        PipelineStep(
            name="satellite-input",
            description="Prepare satellite inputs for every disaster type and audit excluded events.",
            commands=[
                [
                    py,
                    "src/prepare_satellite_batch_input.py",
                    "--input-csv",
                    final_csv,
                    "--output-csv",
                    args.satellite_input_csv,
                    "--disaster-type",
                    args.satellite_disaster_type,
                    "--min-start-date",
                    args.start_date,
                ]
            ],
            required_inputs=[final_csv],
            expected_outputs=[args.satellite_input_csv],
        )
    )

    steps.append(
        PipelineStep(
            name="satellite-general",
            description="Collect general Sentinel-1/2/3 and ESA WorldCover layers for all selected events.",
            commands=[
                [
                    py,
                    "src/fetch_multimodal_satellite_batch.py",
                    "--events-csv",
                    args.satellite_input_csv,
                    "--disaster-type",
                    args.satellite_disaster_type,
                    "--min-start-date",
                    args.start_date,
                    "--limit",
                    str(args.satellite_limit),
                    "--output-dir",
                    args.satellite_output_dir,
                    "--output-resolution-m",
                    str(args.satellite_resolution_m),
                    "--s3-resolution-m",
                    str(args.satellite_s3_resolution_m),
                    "--max-cloud-cover",
                    str(args.satellite_max_cloud_cover),
                    "--window-days",
                    str(args.satellite_window_days),
                    "--sleep-seconds",
                    str(args.satellite_sleep_seconds),
                    "--summary-csv",
                    args.satellite_summary_csv,
                ]
            ],
            required_inputs=[args.satellite_input_csv],
            expected_outputs=[args.satellite_summary_csv],
            note="Requires Copernicus credentials and rasterio; resumes compatible checkpoints. No disaster-specific indices.",
        )
    )

    steps.append(
        PipelineStep(
            name="final-dataset",
            description="Assemble the final complete event-level dataset.",
            commands=[
                [
                    py,
                    "src/assemble_final_event_dataset.py",
                    "--geocoding-csv",
                    final_csv,
                    "--base-csv",
                    args.base_csv,
                    "--weather-progress-csv",
                    args.weather_progress_csv,
                    "--news-progress-csv",
                    args.news_progress_csv,
                    "--summary-csv",
                    args.validated_summary_csv,
                    "--causal-csv",
                    args.normalized_causal_csv,
                    "--satellite-summary-csv",
                    args.satellite_summary_csv,
                    "--satellite-ranking-csv",
                    args.satellite_ranking_csv,
                    "--output-csv",
                    args.final_complete_csv,
                    "--output-summary-csv",
                    args.final_complete_summary_csv,
                ]
            ],
            required_inputs=[final_csv],
            expected_outputs=[args.final_complete_csv, args.final_complete_summary_csv],
        )
    )

    return steps


# Expands group names into ordered step names.
def expand_requested_steps(requested: Iterable[str]) -> List[str]:
    groups = {
        "all": [
            "base-merge",
            "geocoding-analyze",
            "gadm-download",
            "gadm-bboxes",
            "adm2-aois",
            "llm-location",
            "llm-nominatim-queries",
            "llm-nominatim-geocode",
            "geocoding-final",
            "weather",
            "news",
            "summary",
            "summary-validation",
            "causal-chain",
            "causal-type-normalization",
            "satellite-input",
            "satellite-general",
            "final-dataset",
        ],
        "base": ["base-merge"],
        "geocoding": [
            "geocoding-analyze",
            "gadm-download",
            "gadm-bboxes",
            "adm2-aois",
            "llm-location",
            "llm-nominatim-queries",
            "llm-nominatim-geocode",
            "geocoding-final",
        ],
        "weather": ["weather"],
        "reasoning": [
            "news",
            "summary",
            "summary-validation",
            "causal-chain",
            "causal-type-normalization",
        ],
        "satellite": ["satellite-input", "satellite-general"],
        "final": ["final-dataset"],
    }
    expanded: List[str] = []
    for name in requested:
        expanded.extend(groups.get(name, [name]))
    return expanded


# Selects pipeline steps by name while preserving the default order.
def selected_steps(all_steps: Sequence[PipelineStep], requested: Iterable[str]) -> List[PipelineStep]:
    requested_names = expand_requested_steps(requested)
    wanted = set(requested_names)
    available = {step.name for step in all_steps}
    unknown = wanted - available
    if unknown:
        raise SystemExit("Unknown step(s): " + ", ".join(sorted(unknown)))
    return [step for step in all_steps if step.name in wanted]


# Formats a command so it can be copied into a shell if needed.
def format_command(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


# Returns missing required inputs for one step.
def missing_inputs(step: PipelineStep) -> List[str]:
    return [path for path in step.required_inputs if path and not project_path(path).exists()]


# Prints the release pipeline without running heavy API or LLM calls.
def print_plan(steps: Sequence[PipelineStep]) -> None:
    print("Release pipeline plan:")
    for index, step in enumerate(steps, start=1):
        print()
        print(f"{index}. {step.name}")
        print(f"   {step.description}")
        if step.note:
            print(f"   Note: {step.note}")
        missing = missing_inputs(step)
        if missing:
            print("   Missing inputs now: " + ", ".join(missing))
        if step.expected_outputs:
            print("   Outputs: " + ", ".join(step.expected_outputs))
        for command in step.commands:
            print("   $ " + format_command(command))


# Runs the selected steps in order and stops unless continue-on-error is enabled.
def run_steps(steps: Sequence[PipelineStep], *, continue_on_error: bool) -> None:
    for index, step in enumerate(steps, start=1):
        missing = missing_inputs(step)
        if missing:
            raise SystemExit(
                f"Step {step.name!r} is missing input(s): " + ", ".join(missing)
            )

        print()
        print(f"=== [{index}/{len(steps)}] {step.name}: {step.description} ===")
        step_failed = False
        for command in step.commands:
            print("$ " + format_command(command), flush=True)
            completed = subprocess.run(command, cwd=PROJECT_ROOT)
            if completed.returncode != 0:
                message = f"Step {step.name!r} failed with exit code {completed.returncode}"
                if continue_on_error:
                    print(message)
                    step_failed = True
                    break
                raise SystemExit(message)
        if step.expected_outputs and not step_failed:
            print("Step outputs:")
            for output in step.expected_outputs:
                print(f"- {output}")


# Defines the command-line interface for the release pipeline runner.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or print the full release pipeline from raw inputs to final outputs."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run commands. Without this flag the script only prints the plan.",
    )
    parser.add_argument(
        "--steps",
        nargs="+",
        default=["all"],
        help=(
            "Steps or groups to run. Groups: all, base, geocoding, weather, "
            "reasoning, satellite, final."
        ),
    )
    parser.add_argument("--list-steps", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--python", default="")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--emdat-file", default=DEFAULT_EMDAT_FILE)
    parser.add_argument("--gdis-file", default=DEFAULT_GDIS_FILE)
    parser.add_argument("--base-csv", default=DEFAULT_BASE_CSV)
    parser.add_argument("--gadm-dir", default="data/gadm")
    parser.add_argument(
        "--gadm-plan-csv",
        default="results/recent_emdat_geocoding/gadm_download_plan.csv",
    )
    parser.add_argument("--gadm-top-countries", type=int, default=0)
    parser.add_argument("--gadm-sleep-seconds", type=float, default=5.0)
    parser.add_argument(
        "--geocoding-candidates-csv",
        default="results/recent_emdat_geocoding/candidates.csv",
    )
    parser.add_argument(
        "--geocoding-summary-csv",
        default="results/recent_emdat_geocoding/summary.csv",
    )
    parser.add_argument("--admin-units-csv", default=DEFAULT_ADMIN_UNITS_CSV)
    parser.add_argument("--admin-unit-bboxes-csv", default=DEFAULT_ADMIN_UNIT_BBOXES_CSV)
    parser.add_argument("--adm2-aois-csv", default=DEFAULT_ADM2_AOIS_CSV)
    parser.add_argument("--location-model-name", default=DEFAULT_LOCATION_MODEL)
    parser.add_argument("--location-limit", type=int, default=0)
    parser.add_argument("--location-max-new-tokens", type=int, default=32)
    parser.add_argument("--llm-location-csv", default=DEFAULT_LLM_LOCATION_CSV)
    parser.add_argument(
        "--llm-nominatim-queries-csv",
        default=DEFAULT_LLM_NOMINATIM_QUERIES_CSV,
    )
    parser.add_argument(
        "--llm-nominatim-queries-summary-csv",
        default=(
            "results/recent_emdat_geocoding/"
            "llm_location_nominatim_queries_new_prompt_all_70b_from_2014-04-03_summary.csv"
        ),
    )
    parser.add_argument("--llm-nominatim-results-csv", default=DEFAULT_LLM_NOMINATIM_RESULTS_CSV)
    parser.add_argument("--nominatim-limit", type=int, default=0)
    parser.add_argument("--nominatim-sleep-seconds", type=float, default=1.2)
    parser.add_argument("--nominatim-variant-sleep-seconds", type=float, default=1.2)
    parser.add_argument("--final-geocoding-csv", default=DEFAULT_FINAL_GEOCODING_CSV)
    parser.add_argument("--final-positions-csv", default=DEFAULT_FINAL_POSITIONS_CSV)
    parser.add_argument("--final-position-summary-csv", default=DEFAULT_FINAL_POSITION_SUMMARY_CSV)
    parser.add_argument("--review-audit-csv", default=DEFAULT_REVIEW_AUDIT_CSV)
    parser.add_argument("--review-summary-csv", default=DEFAULT_REVIEW_SUMMARY_CSV)
    parser.add_argument("--weather-json", default=DEFAULT_WEATHER_JSON)
    parser.add_argument("--weather-progress-csv", default=DEFAULT_WEATHER_PROGRESS_CSV)
    parser.add_argument("--weather-limit", type=int, default=0)
    parser.add_argument("--weather-sleep-seconds", type=float, default=0.2)
    parser.add_argument("--weather-max-retries", type=int, default=1)
    parser.add_argument("--news-json", default=DEFAULT_NEWS_JSON)
    parser.add_argument("--news-progress-csv", default=DEFAULT_NEWS_PROGRESS_CSV)
    parser.add_argument("--news-limit", type=int, default=0)
    parser.add_argument("--news-sleep-seconds", type=float, default=3.0)
    parser.add_argument("--summary-jsonl", default=DEFAULT_SUMMARY_JSONL)
    parser.add_argument("--summary-csv", default=DEFAULT_SUMMARY_CSV)
    parser.add_argument("--summary-coverage-csv", default=DEFAULT_SUMMARY_COVERAGE_CSV)
    parser.add_argument("--validated-summary-jsonl", default=DEFAULT_VALIDATED_SUMMARY_JSONL)
    parser.add_argument("--validated-summary-csv", default=DEFAULT_VALIDATED_SUMMARY_CSV)
    parser.add_argument(
        "--validated-summary-coverage-csv",
        default=DEFAULT_VALIDATED_SUMMARY_COVERAGE_CSV,
    )
    parser.add_argument("--summary-limit", type=int, default=0)
    parser.add_argument("--summary-max-new-tokens", type=int, default=256)
    parser.add_argument("--summary-model-name", default=DEFAULT_SUMMARY_MODEL)
    parser.add_argument("--causal-jsonl", default=DEFAULT_CAUSAL_JSONL)
    parser.add_argument("--causal-csv", default=DEFAULT_CAUSAL_CSV)
    parser.add_argument("--causal-coverage-csv", default=DEFAULT_CAUSAL_COVERAGE_CSV)
    parser.add_argument("--causal-limit", type=int, default=0)
    parser.add_argument("--causal-chain-max-new-tokens", type=int, default=512)
    parser.add_argument("--causal-model-name", default=DEFAULT_CAUSAL_MODEL)
    parser.add_argument("--normalized-causal-jsonl", default=DEFAULT_NORMALIZED_CAUSAL_JSONL)
    parser.add_argument("--normalized-causal-csv", default=DEFAULT_NORMALIZED_CAUSAL_CSV)
    parser.add_argument("--type-map-csv", default=DEFAULT_TYPE_MAP_CSV)
    parser.add_argument("--type-summary-csv", default=DEFAULT_TYPE_SUMMARY_CSV)
    parser.add_argument(
        "--llm-quantization",
        choices=["4bit", "8bit", "none"],
        default="4bit",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate LLM JSONL outputs instead of resuming existing ones.",
    )
    parser.add_argument("--satellite-input-csv", default=DEFAULT_SATELLITE_INPUT_CSV)
    parser.add_argument("--satellite-summary-csv", default=DEFAULT_SATELLITE_SUMMARY_CSV)
    parser.add_argument("--satellite-ranking-csv", default=DEFAULT_SATELLITE_RANKING_CSV)
    parser.add_argument("--satellite-output-dir", default="results/multimodal_satellite_2014_plus")
    parser.add_argument("--satellite-disaster-type", default="", help="Optional type filter; defaults to all types, including Flood")
    parser.add_argument("--satellite-limit", type=int, default=0)
    parser.add_argument("--satellite-resolution-m", type=int, default=20)
    parser.add_argument("--satellite-s3-resolution-m", type=int, default=1000)
    parser.add_argument("--satellite-max-cloud-cover", type=float, default=100.0)
    parser.add_argument("--satellite-window-days", type=int, default=10)
    parser.add_argument("--satellite-sleep-seconds", type=float, default=2.0)
    parser.add_argument("--final-complete-csv", default=DEFAULT_FINAL_COMPLETE_CSV)
    parser.add_argument(
        "--final-complete-summary-csv",
        default=DEFAULT_FINAL_COMPLETE_SUMMARY_CSV,
    )
    return parser.parse_args()


# Entry point for printing or executing the selected release pipeline.
def main() -> None:
    args = parse_args()
    steps = build_steps(args)
    if args.list_steps:
        print("Step groups: all, base, geocoding, weather, reasoning, satellite, final")
        print()
        for step in steps:
            print(f"{step.name}: {step.description}")
        return

    steps_to_run = selected_steps(steps, args.steps)
    if not args.execute:
        print_plan(steps_to_run)
        print()
        print("No commands were executed. Add --execute to run the selected steps.")
        return

    run_steps(steps_to_run, continue_on_error=args.continue_on_error)


if __name__ == "__main__":
    main()
