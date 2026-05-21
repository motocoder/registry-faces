"""Provider registry + factory.

Presets are friendly names like "ollama" or "groq" that fix a default model,
base URL, and credential env var. The CLI and `build_adapter_from_url()` both
take a preset name; users can override the model with --model or
REGISTRY_FACES_MODEL.

Add a new free-tier service: append to PRESETS. Add a fundamentally new API:
add a Provider subclass + an entry in _IMPL_MAP.
"""

from __future__ import annotations

import os
from typing import Any

from .anthropic_provider import AnthropicProvider
from .base import Provider
from .gemini_provider import GeminiProvider
from .openai_compatible import OpenAICompatibleProvider

# Each preset specifies which implementation to use and its defaults.
# `api_key_env` is the env var to read the API key from (None = no key needed).
PRESETS: dict[str, dict[str, Any]] = {
    # Highest quality, paid.
    "anthropic": {
        "impl": "anthropic",
        "model": "claude-opus-4-7",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    # Generous free tier, long context, decent tool use.
    "gemini": {
        "impl": "gemini",
        "model": "gemini-2.0-flash",
        "api_key_env": "GOOGLE_API_KEY",
    },
    # Plain OpenAI (paid).
    "openai": {
        "impl": "openai",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    # Fully local, no key. Requires `ollama serve`.
    "ollama": {
        "impl": "openai",
        "model": "qwen2.5-coder:32b",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": None,
    },
    # Free tier, very fast inference.
    "groq": {
        "impl": "openai",
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
    },
    # Free tier, daily quota.
    "cerebras": {
        "impl": "openai",
        "model": "llama-3.3-70b",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
    },
    # Aggregator with several free models tagged ":free".
    "openrouter": {
        "impl": "openai",
        "model": "deepseek/deepseek-chat-v3",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    # Free with GitHub account, rate-limited.
    "github-models": {
        "impl": "openai",
        "model": "gpt-4o-mini",
        "base_url": "https://models.inference.ai.azure.com",
        "api_key_env": "GITHUB_TOKEN",
    },
}

_IMPL_MAP = {
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "openai": OpenAICompatibleProvider,
}


def list_presets() -> list[str]:
    return list(PRESETS.keys())


def get_provider(
    preset: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Provider:
    """Resolve a preset + optional overrides into a configured Provider.

    Resolution order (later wins):
      1. preset defaults
      2. REGISTRY_FACES_MODEL / _BASE_URL env vars
      3. explicit arguments
    """
    preset_name = preset or os.environ.get("REGISTRY_FACES_PROVIDER", "anthropic")
    if preset_name not in PRESETS:
        raise ValueError(
            f"Unknown provider preset: {preset_name!r}. "
            f"Choose one of: {', '.join(list_presets())}"
        )
    cfg = dict(PRESETS[preset_name])

    if "REGISTRY_FACES_MODEL" in os.environ:
        cfg["model"] = os.environ["REGISTRY_FACES_MODEL"]
    if "REGISTRY_FACES_BASE_URL" in os.environ:
        cfg["base_url"] = os.environ["REGISTRY_FACES_BASE_URL"]
    if model is not None:
        cfg["model"] = model
    if base_url is not None:
        cfg["base_url"] = base_url

    resolved_key = api_key
    if resolved_key is None and cfg.get("api_key_env"):
        resolved_key = os.environ.get(cfg["api_key_env"])

    impl_name = cfg["impl"]
    impl_cls = _IMPL_MAP.get(impl_name)
    if impl_cls is None:
        raise ValueError(f"Unknown impl: {impl_name!r}")

    kwargs: dict[str, Any] = {"model": cfg["model"], "api_key": resolved_key}
    if impl_name == "openai":
        kwargs["base_url"] = cfg.get("base_url")

    return impl_cls(**kwargs)


__all__ = [
    "Provider",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "PRESETS",
    "get_provider",
    "list_presets",
]
