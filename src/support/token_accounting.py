"""Recover text token counts without loading model weights or generating responses."""

import math

from support.final_dataset_records import missing, number, token_usage
from support.reasoner import format_reasoner_prompt


CHARACTERS_PER_TOKEN = 4.0


class TokenUsageRecovery:
    def __init__(self):
        self.tokenizers = {}

    # Loads each tokenizer once from the local cache, without remote code or downloads.
    def tokenizer(self, model_name):
        if missing(model_name):
            return None
        if model_name not in self.tokenizers:
            try:
                from transformers import AutoTokenizer

                self.tokenizers[model_name] = AutoTokenizer.from_pretrained(
                    model_name, local_files_only=True, trust_remote_code=False,
                )
            except (ImportError, OSError, ValueError):
                self.tokenizers[model_name] = None
        return self.tokenizers[model_name]

    # Separates saved counts, retokenized text and rough character estimates.
    def recover(self, row, stage, warnings):
        row = row or {}
        usage = token_usage(row)
        status = str(row.get("llm_call_status") or "")
        if status == "dry_run" or status.startswith("skipped_"):
            return {**usage, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "input_method": "not_called", "output_method": "not_called", "total_method": "not_called"}
        if status != "called":
            return usage
        prompt = row.get(stage + "_prompt")
        response = row.get(stage + "_raw_response")
        prompt = str(prompt).strip() if not missing(prompt) else None
        response = str(response) if not missing(response) else None
        need_input = usage["input_tokens"] is None
        need_output = usage["output_tokens"] is None
        tokenizer = self.tokenizer(row.get("model_name")) if (
            need_input and prompt is not None or need_output and response is not None
        ) else None
        if tokenizer is not None:
            usage["tokenizer_name"] = row.get("model_name")
            usage["tokenizer_revision"] = getattr(tokenizer, "init_kwargs", {}).get("_commit_hash")
            if need_input and prompt is not None:
                formatted = format_reasoner_prompt(tokenizer, prompt)
                usage["input_tokens"] = len(tokenizer(formatted)["input_ids"])
                usage["input_method"] = "retokenized_saved_prompt"
            if need_output and response is not None:
                usage["output_tokens"] = len(tokenizer(response, add_special_tokens=False)["input_ids"])
                usage["output_method"] = "retokenized_saved_response"
        elif (need_input and prompt is not None) or (need_output and response is not None):
            warnings.append(f"tokenizer_unavailable:{stage}:{row.get('model_name') or 'unknown'}")

        estimates = {}
        if usage["input_tokens"] is None:
            if prompt is not None:
                input_chars = len(prompt)
                scope = "saved_prompt_without_chat_template"
            else:
                input_chars = number(row.get("news_total_chars"), count=True)
                scope = "news_context_only_excludes_instructions_metadata_and_chat_template"
            if input_chars is not None:
                estimates.update(input_tokens=math.ceil(input_chars / CHARACTERS_PER_TOKEN), input_scope=scope)
        if usage["output_tokens"] is None and response is not None:
            estimates["output_tokens"] = math.ceil(len(response) / CHARACTERS_PER_TOKEN)
        if estimates:
            usage["estimates"] = {"method": "character_heuristic", "characters_per_token": CHARACTERS_PER_TOKEN,
                                  **estimates}
        if usage["total_tokens"] is None and all(usage[key] is not None for key in ("input_tokens", "output_tokens")):
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
            usage["total_method"] = "sum_of_available_counts"
        return usage
