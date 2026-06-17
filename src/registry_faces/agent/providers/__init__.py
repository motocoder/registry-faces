"""Provider registry — registry-faces binding over the framework providers.

Re-exports the shared registry and binds ``get_provider`` to the
``REGISTRY_FACES_*`` env-var prefix so ``REGISTRY_FACES_MODEL`` /
``REGISTRY_FACES_PROVIDER`` / ``REGISTRY_FACES_BASE_URL`` continue to work.
"""

from __future__ import annotations

from web_scrubber.agent.providers import (  # noqa: F401
    PRESETS,
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    Provider,
    list_presets,
)
from web_scrubber.agent.providers import get_provider as _get_provider

ENV_PREFIX = "REGISTRY_FACES"


def get_provider(preset=None, model=None, api_key=None, base_url=None):
    return _get_provider(
        preset=preset,
        model=model,
        api_key=api_key,
        base_url=base_url,
        env_prefix=ENV_PREFIX,
    )


__all__ = [
    "PRESETS",
    "Provider",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "get_provider",
    "list_presets",
    "ENV_PREFIX",
]
