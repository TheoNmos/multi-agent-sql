from __future__ import annotations

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from app.config import settings

llama_4_scout = OpenAIChatModel(
    model_name="meta-llama/llama-4-scout",
    provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
)

gpt_4o_mini = OpenAIChatModel(
    model_name="openai/gpt-4o-mini",
    provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
)


gemini_2_5_flash = OpenAIChatModel(
    model_name="google/gemini-2.5-flash",
    provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
)

gpt_5 = OpenAIChatModel(
    model_name="openai/gpt-5-mini",
    provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
)

gpt_5_mini = gpt_5  # Alias for clarity

grok_4_fast = OpenAIChatModel(
    model_name="x-ai/grok-4-fast",
    provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
)

# Non-reasoning version of grok_4_fast (faster, no thinking tokens)
# Uses the same model but configured to disable reasoning via extra_body
grok_4_fast_no_reasoning = OpenAIChatModel(
    model_name="x-ai/grok-4-fast",
    provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
)
