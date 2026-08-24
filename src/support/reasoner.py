from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


DEFAULT_REASONER_MODEL = "meta-llama/Llama-3.1-70B-Instruct"


@dataclass
class ReasonerConfig:
    model_name: str = DEFAULT_REASONER_MODEL
    device_map: Optional[str] = "auto"
    torch_dtype: Optional[str] = "auto"
    trust_remote_code: bool = False
    token: Optional[str] = None
    load_in_4bit: bool = False
    load_in_8bit: bool = False
    max_new_tokens: int = 128
    do_sample: bool = False
    generation_kwargs: Dict[str, Any] = field(default_factory=dict)


class Reasoner:
    """Small reusable HuggingFace LLM wrapper for text-only reasoning tasks."""

    def __init__(
        self,
        model_name: str = DEFAULT_REASONER_MODEL,
        *,
        device_map: Optional[str] = "auto",
        torch_dtype: Optional[str] = "auto",
        trust_remote_code: bool = False,
        token: Optional[str] = None,
        load_in_4bit: bool = False,
        load_in_8bit: bool = False,
        max_new_tokens: int = 128,
        do_sample: bool = False,
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        if load_in_4bit and load_in_8bit:
            raise ValueError("load_in_4bit and load_in_8bit are mutually exclusive")
        self.config = ReasonerConfig(
            model_name=model_name,
            device_map=device_map,
            torch_dtype=torch_dtype,
            trust_remote_code=trust_remote_code,
            token=token,
            load_in_4bit=load_in_4bit,
            load_in_8bit=load_in_8bit,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
            generation_kwargs=generation_kwargs or {},
        )
        self._tokenizer = None
        self._model = None

    # Returns the HuggingFace model name configured for this reasoner.
    @property
    def model_name(self) -> str:
        return self.config.model_name

    # Loads the model on first use and returns its answer for one prompt.
    def ask(
        self,
        prompt: str,
        *,
        max_new_tokens: Optional[int] = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ) -> str:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        self._load_model()
        tokenizer = self._tokenizer
        model = self._model
        prompt_text = self._format_prompt(prompt)

        inputs = tokenizer(prompt_text, return_tensors="pt")
        device = getattr(model, "device", None)
        if device is not None:
            inputs = {key: value.to(device) for key, value in inputs.items()}

        generate_kwargs: Dict[str, Any] = {
            "max_new_tokens": max_new_tokens or self.config.max_new_tokens,
            "do_sample": self.config.do_sample,
        }
        if tokenizer.eos_token_id is not None:
            generate_kwargs.setdefault("eos_token_id", tokenizer.eos_token_id)
            generate_kwargs.setdefault("pad_token_id", tokenizer.eos_token_id)
        generate_kwargs.update(self.config.generation_kwargs)
        if generation_kwargs:
            generate_kwargs.update(generation_kwargs)

        output_ids = model.generate(**inputs, **generate_kwargs)
        prompt_length = inputs["input_ids"].shape[-1]
        response_ids = output_ids[0][prompt_length:]
        return tokenizer.decode(response_ids, skip_special_tokens=True).strip()

    # Loads tokenizer and model lazily to avoid startup cost in dry runs.
    def _load_model(self) -> None:
        if self._model is not None and self._tokenizer is not None:
            return

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Reasoner requires HuggingFace transformers and PyTorch. "
                "Install project requirements on the cluster before using it."
            ) from exc

        load_kwargs: Dict[str, Any] = {
            "trust_remote_code": self.config.trust_remote_code,
        }
        if self.config.device_map:
            load_kwargs["device_map"] = self.config.device_map
        if self.config.token:
            load_kwargs["token"] = self.config.token
        if self.config.torch_dtype:
            if self.config.torch_dtype == "auto":
                load_kwargs["torch_dtype"] = "auto"
            else:
                load_kwargs["torch_dtype"] = getattr(torch, self.config.torch_dtype)
        if self.config.load_in_4bit or self.config.load_in_8bit:
            try:
                from transformers import BitsAndBytesConfig
            except ImportError as exc:
                raise RuntimeError(
                    "Quantized Reasoner loading requires bitsandbytes support. "
                    "Install project requirements on the cluster before using "
                    "--load-in-4bit or --load-in-8bit."
                ) from exc

            if self.config.load_in_4bit:
                load_kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.bfloat16,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_use_double_quant=True,
                )
            else:
                load_kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_8bit=True,
                )

        tokenizer_kwargs: Dict[str, Any] = {
            "trust_remote_code": self.config.trust_remote_code,
        }
        if self.config.token:
            tokenizer_kwargs["token"] = self.config.token

        self._tokenizer = AutoTokenizer.from_pretrained(
            self.config.model_name,
            **tokenizer_kwargs,
        )
        if self._tokenizer.pad_token_id is None and self._tokenizer.eos_token_id is not None:
            self._tokenizer.pad_token = self._tokenizer.eos_token

        self._model = AutoModelForCausalLM.from_pretrained(
            self.config.model_name,
            **load_kwargs,
        )
        self._model.eval()

    # Applies the model chat template when the tokenizer provides one.
    def _format_prompt(self, prompt: str) -> str:
        tokenizer = self._tokenizer
        if getattr(tokenizer, "chat_template", None):
            messages = [{"role": "user", "content": prompt}]
            return tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
        return prompt
