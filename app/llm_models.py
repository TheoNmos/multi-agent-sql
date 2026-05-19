from __future__ import annotations

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider

from app.config import settings


def _or(model_name: str, model_settings: OpenAIChatModelSettings | None = None) -> OpenAIChatModel:
    """Create an OpenRouter-backed model."""
    # OpenAIChatModelSettings is a TypedDict; the constructor returns a plain dict.
    base_settings: OpenAIChatModelSettings = OpenAIChatModelSettings(timeout=settings.llm_request_timeout_s)
    merged: OpenAIChatModelSettings = (
        {**base_settings, **model_settings} if model_settings is not None else base_settings
    )
    return OpenAIChatModel(
        model_name=model_name,
        provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
        settings=merged,
    )


# ---------------------------------------------------------------------------
# Legacy / benchmark references (kept for single-agent and benchmarks)
# ---------------------------------------------------------------------------

llama_4_scout = _or("meta-llama/llama-4-scout")
gpt_4o_mini = _or("openai/gpt-4o-mini")
gemini_2_5_flash = _or("google/gemini-2.5-flash")
gpt_5_mini = _or("openai/gpt-5-mini")
gpt_5_mini_minimal = _or(
    "openai/gpt-5-mini",
    model_settings=OpenAIChatModelSettings(openai_reasoning_effort="minimal"),
)
gpt_5 = gpt_5_mini  # backward-compat alias

# ---------------------------------------------------------------------------
# Multi-agent pipeline: per-role models
#
# Interpreter / mapper / validator → GPT-5 mini (minimal reasoning) for speed.
# Generator → GPT-5 mini (default reasoning) for higher-quality SQL synthesis.
# ---------------------------------------------------------------------------

interpreter_model = gpt_5_mini_minimal
mapper_model = gpt_5_mini_minimal
generator_model = gpt_5_mini
validator_model = gpt_5_mini_minimal
