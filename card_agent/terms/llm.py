"""Thin LLM provider interface for structured extraction.

Only OpenAI is implemented. Selection is by environment:
    LLM_PROVIDER   openai (default) | anthropic (interface only, not implemented)
    LLM_MODEL      default gpt-6-luna (OpenAI's cheapest current tier with
                   Structured Outputs; set the repo variable to change it)
    OPENAI_API_KEY required for openai; without it the pipeline skips extraction
    LLM_REASONING_EFFORT  optional (e.g. low); omitted -> the model's default
    LLM_PRICE_INPUT_PER_MTOK / LLM_PRICE_OUTPUT_PER_MTOK  override the price table

The model gets no tools: one request, JSON out, parsed into the Pydantic schema.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

DEFAULT_OPENAI_MODEL = "gpt-6-luna"

# USD per 1M tokens (input, output), standard tier, as published 2026-09.
# Unknown models report cost as unknown unless LLM_PRICE_* is set.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
}


class LLMError(RuntimeError):
    """The provider failed (network, API error, refusal, unparseable output).

    `usage` holds whatever tokens the failed call still consumed. `fatal` means
    every further call in this run would fail the same way (bad key, no access,
    unknown model), so the pipeline stops calling."""

    def __init__(self, message: str, usage: Usage | None = None, fatal: bool = False):
        super().__init__(message)
        self.usage = usage if usage is not None else Usage()
        self.fatal = fatal


# HTTP statuses after which no call in this run can succeed.
FATAL_STATUS = {
    401: "OpenAI rejected the API key (HTTP 401). Check the OPENAI_API_KEY secret.",
    403: "OpenAI refused access for this key (HTTP 403). Check the key's project permissions.",
    404: "OpenAI has no model {model!r} for this key (HTTP 404). Check the LLM_MODEL variable.",
}


def redact(text: str) -> str:
    """Drop anything that looks like an API key (even a masked one) from a message."""
    return re.sub(r"\bsk-[^\s'\",]+", "sk-[redacted]", text)


class ProviderUnavailable(RuntimeError):
    """No usable provider configured (e.g. missing API key)."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens


def price_for(model: str) -> tuple[float, float] | None:
    env_in = os.environ.get("LLM_PRICE_INPUT_PER_MTOK")
    env_out = os.environ.get("LLM_PRICE_OUTPUT_PER_MTOK")
    if env_in and env_out:
        return float(env_in), float(env_out)
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    # Dated snapshots ("gpt-5-mini-2025-08-07") price like their base model.
    for known, price in PRICES_PER_MTOK.items():
        if model.startswith(f"{known}-"):
            return price
    return None


def estimate_cost(usage: Usage, model: str) -> float | None:
    """USD, at standard rates. Reasoning tokens are billed as output tokens and
    are already included in output_tokens. Cached-input discounts are ignored,
    so this errs high."""
    price = price_for(model)
    if price is None:
        return None
    return (usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1_000_000


@dataclass
class LLMResult:
    parsed: BaseModel
    usage: Usage
    model: str
    raw: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str
    model: str

    def extract(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult: ...


class OpenAIProvider:
    """OpenAI Responses API with Structured Outputs (strict JSON schema generated
    from the Pydantic model by the SDK)."""

    name = "openai"

    def __init__(
        self, api_key: str, model: str, client: Any = None, reasoning_effort: str | None = None
    ):
        self.model = model
        self.reasoning_effort = reasoning_effort
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, timeout=180, max_retries=2)
        self.client = client

    def extract(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        kwargs: dict[str, Any] = {}
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=system,
                input=user,
                text_format=schema,
                **kwargs,
            )
        except Exception as exc:  # the SDK raises many types; report them uniformly
            status = getattr(exc, "status_code", None)
            if status in FATAL_STATUS:
                message = FATAL_STATUS[status].format(model=self.model)
                raise LLMError(message, fatal=True) from exc
            raise LLMError(f"{type(exc).__name__}: {redact(str(exc))}") from exc

        usage = Usage(calls=1)
        if response.usage is not None:
            usage.input_tokens = response.usage.input_tokens or 0
            usage.output_tokens = response.usage.output_tokens or 0
            details = getattr(response.usage, "input_tokens_details", None)
            usage.cached_input_tokens = getattr(details, "cached_tokens", 0) or 0
            out_details = getattr(response.usage, "output_tokens_details", None)
            usage.reasoning_tokens = getattr(out_details, "reasoning_tokens", 0) or 0

        parsed = response.output_parsed
        if parsed is None:
            raise LLMError(f"no structured output ({_refusal_or_status(response)})", usage)
        return LLMResult(parsed=parsed, usage=usage, model=getattr(response, "model", self.model))


def _refusal_or_status(response: Any) -> str:
    for output in getattr(response, "output", None) or []:
        for content in getattr(output, "content", None) or []:
            if getattr(content, "type", "") == "refusal":
                return f"refusal: {getattr(content, 'refusal', '')}"
    details = getattr(response, "incomplete_details", None)
    if details is not None:
        return f"incomplete: {getattr(details, 'reason', details)}"
    return f"status {getattr(response, 'status', 'unknown')}"


def get_provider(env: dict[str, str] | None = None) -> Provider:
    """The configured provider, or ProviderUnavailable with a clear reason."""
    env = dict(os.environ if env is None else env)
    name = (env.get("LLM_PROVIDER") or "openai").strip().lower()
    if name == "openai":
        key = env.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ProviderUnavailable(
                "OPENAI_API_KEY is not set; skipping LLM extraction (pages were still "
                "fetched and hashed, and unchanged pages were re-verified)."
            )
        model = (env.get("LLM_MODEL") or "").strip() or DEFAULT_OPENAI_MODEL
        effort = (env.get("LLM_REASONING_EFFORT") or "").strip() or None
        return OpenAIProvider(api_key=key, model=model, reasoning_effort=effort)
    if name == "anthropic":
        # Interface placeholder: set LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY once
        # an AnthropicProvider with the same extract() signature is added here.
        raise ProviderUnavailable("LLM_PROVIDER=anthropic is not implemented yet.")
    raise ProviderUnavailable(f"Unknown LLM_PROVIDER {name!r}.")
