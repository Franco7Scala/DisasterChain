import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import export_final_event_dataset as export
from support.event_news_summary import extract_causal_chain_from_news, summarize_event_from_news
from support.final_dataset_records import public_values, read_records
from support.reasoner import Reasoner
from support.token_accounting import TokenUsageRecovery
from test_final_dataset_export import EVENT, causal_row, csv_file, fixture


class FakeTensor:
    def __init__(self, values):
        self.values = values
        self.shape = (1, len(values))


class FakeTokenizer:
    chat_template = "test-template"
    eos_token_id = 99
    init_kwargs = {"_commit_hash": "test-revision"}

    def apply_chat_template(self, messages, **kwargs):
        return "<user> " + messages[0]["content"] + " <assistant>"

    def __call__(self, text, add_special_tokens=True, return_tensors=None):
        ids = [1] * (len(text.split()) + int(add_special_tokens))
        return {"input_ids": FakeTensor(ids) if return_tensors else ids}

    def decode(self, ids, **kwargs):
        return "  A saved response.  "


class TokenRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.counter = TokenUsageRecovery()
        self.counter.tokenizers["test-model"] = FakeTokenizer()
        self.row = {"model_name": "test-model", "llm_call_status": "called", "summary_prompt": "  Two words  ",
                    "summary_raw_response": "Summary: a response", "news_total_chars": 101}

    def test_saved_text_recount_uses_chat_template_and_raw_response(self):
        result = self.counter.recover(self.row, "summary", [])
        self.assertEqual(result["input_tokens"], 5)
        self.assertEqual(result["output_tokens"], 3)
        self.assertEqual(result["total_tokens"], 8)
        self.assertEqual(result["input_method"], "retokenized_saved_prompt")
        self.assertEqual(result["output_method"], "retokenized_saved_response")
        self.assertEqual(result["tokenizer_revision"], "test-revision")
        self.assertIsNone(result["estimates"])
        self.assertNotIn("Summary:", json.dumps(result))

    def test_missing_prompt_has_news_only_estimate_not_false_full_input_count(self):
        del self.row["summary_prompt"]
        result = self.counter.recover(self.row, "summary", [])
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertEqual(result["output_tokens"], 3)
        self.assertEqual(result["estimates"]["input_tokens"], 26)
        self.assertIn("news_context_only", result["estimates"]["input_scope"])
        self.assertEqual(result["estimates"]["characters_per_token"], 4.0)

    def test_unavailable_tokenizer_falls_back_to_explicit_estimates(self):
        self.counter.tokenizers["test-model"] = None
        notes = []
        result = self.counter.recover(self.row, "summary", notes)
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["output_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertEqual(result["estimates"]["input_tokens"], 3)
        self.assertEqual(result["estimates"]["input_scope"], "saved_prompt_without_chat_template")
        self.assertEqual(result["estimates"]["output_tokens"], 5)
        self.assertIn("tokenizer_unavailable:summary:test-model", notes)

    def test_recorded_counts_take_priority_and_zero_is_not_missing(self):
        self.row.update(input_tokens=10, output_tokens=0, total_tokens=10, token_count_method="generated_token_ids")
        with patch.object(self.counter, "tokenizer", side_effect=AssertionError("No recount needed")):
            result = self.counter.recover(self.row, "summary", [])
        self.assertEqual(result["input_tokens"], 10)
        self.assertEqual(result["output_tokens"], 0)
        self.assertEqual(result["total_tokens"], 10)
        self.assertEqual(result["output_method"], "generated_token_ids")
        self.assertIsNone(result["estimates"])

    def test_no_tokenizer_or_estimates_when_not_called_or_unknown(self):
        for status in ("dry_run", "skipped_insufficient_news", "skipped_no_accepted_summary", ""):
            with self.subTest(status=status), patch.object(self.counter, "tokenizer", side_effect=AssertionError("No call")):
                result = self.counter.recover({**self.row, "llm_call_status": status}, "summary", [])
                self.assertIsNone(result["estimates"])
                self.assertEqual(result["total_tokens"], 0 if status else None)

    def test_never_counts_cleaned_summary_or_normalized_chain_as_raw_output(self):
        for stage, extra in (("summary", {"event_summary": "Clean text."}),
                             ("causal_chain", {"causal_chain_json": '{"causal_chain": []}'})):
            result = self.counter.recover({"llm_call_status": "called", "model_name": "test-model", **extra}, stage, [])
            self.assertIsNone(result["output_tokens"])
            self.assertIsNone(result["estimates"])

    def test_loader_is_cache_only_and_never_loads_weights(self):
        auto = Mock()
        auto.from_pretrained.return_value = FakeTokenizer()
        with patch.dict(sys.modules, {"transformers": SimpleNamespace(AutoTokenizer=auto)}), patch("socket.socket", side_effect=AssertionError("No network")):
            self.counter.tokenizer("cached-model")
            self.counter.tokenizer("cached-model")
        auto.from_pretrained.assert_called_once_with("cached-model", local_files_only=True, trust_remote_code=False)

    def test_unavailable_tokenizer_is_not_retried_for_every_event(self):
        auto = Mock()
        auto.from_pretrained.side_effect = OSError("Not in cache")
        with patch.dict(sys.modules, {"transformers": SimpleNamespace(AutoTokenizer=auto)}):
            self.assertIsNone(self.counter.tokenizer("absent"))
            self.assertIsNone(self.counter.tokenizer("absent"))
        self.assertEqual(auto.from_pretrained.call_count, 1)

    def test_export_recovery_leaves_sources_unchanged_and_publishes_no_raw_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args, *_ = fixture(root)
            args.recover_tokens = True
            csv_file(root / "summary.csv", [{"event_id": EVENT, "event_summary": "Accepted text.",
                      "summary_validation_status": "accepted", **self.row}])
            csv_file(root / "causal.csv", [{**causal_row(), "llm_call_status": "called", "model_name": "test-model",
                                           "causal_chain_prompt": "", "causal_chain_raw_response": "Raw causal response",
                                           "news_total_chars": 100}])
            before = {path: path.read_bytes() for path in (root / "summary.csv", root / "causal.csv")}
            with patch.object(export, "TokenUsageRecovery", return_value=self.counter), patch("socket.socket", side_effect=AssertionError("No network")), redirect_stdout(io.StringIO()):
                result = export.build_export(args)
                export.write_package(args, result)
            event = read_records(root / "release/data/dataset.json")[EVENT]
            self.assertEqual(event["summary"]["token_usage"]["total_tokens"], 8)
            self.assertEqual(event["causal_chain"]["token_usage"]["estimates"]["input_tokens"], 25)
            self.assertNotIn("raw_response", json.dumps(event))
            self.assertEqual(before, {path: path.read_bytes() for path in before})


class GenerationAccountingTests(unittest.TestCase):
    def test_reasoner_records_generated_ids_including_end_token(self):
        reasoner = Reasoner("test-model")
        reasoner._tokenizer = FakeTokenizer()
        reasoner._model = SimpleNamespace(generate=lambda **kwargs: [[*kwargs["input_ids"].values, 7, 99]])
        with patch.object(reasoner, "_load_model"), patch("socket.socket", side_effect=AssertionError("No network")):
            text = reasoner.ask("Two words")
        self.assertEqual(text, "A saved response.")
        self.assertEqual(reasoner.last_token_usage["input_tokens"], 5)
        self.assertEqual(reasoner.last_token_usage["output_tokens"], 2)
        self.assertEqual(reasoner.last_token_usage["total_tokens"], 7)
        self.assertEqual(reasoner.last_token_usage["token_count_method"], "generated_token_ids")
        with self.assertRaises(ValueError):
            reasoner.ask("")
        self.assertEqual(reasoner.last_token_usage, {})

    def test_summary_and_causal_outputs_preserve_actual_usage(self):
        usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                 "token_count_method": "generated_token_ids"}
        for function, response in ((summarize_event_from_news, "A supported summary."),
                                   (extract_causal_chain_from_news, '{"causal_chain": []}')):
            reasoner = SimpleNamespace(ask=lambda *args, **kwargs: response, last_token_usage=usage)
            output = function(reasoner, {})
            for key, value in usage.items():
                self.assertEqual(output[key], value)


if __name__ == "__main__":
    unittest.main()
