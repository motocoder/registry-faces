"""Anthropic Claude provider.

Uses the beta `tool_runner` which handles the entire tool-use loop internally.
Adaptive thinking + high effort: this is the highest-quality option for the
adapter-generation task.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import Provider

try:
    import anthropic
    from anthropic import beta_tool

    _AVAILABLE = True
    _IMPORT_ERROR: Exception | None = None
except ImportError as e:
    _AVAILABLE = False
    _IMPORT_ERROR = e


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None) -> None:
        if not _AVAILABLE:
            raise RuntimeError(
                "Anthropic SDK not installed. Run: pip install 'registry-faces[anthropic]'"
            ) from _IMPORT_ERROR
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def run_agent(
        self,
        *,
        system: str,
        user_prompt: str,
        tools: list[Callable],
        max_iterations: int = 15,
    ) -> str:
        decorated = [beta_tool(fn) for fn in tools]
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=decorated,
            messages=[{"role": "user", "content": user_prompt}],
            max_iterations=max_iterations,
        )

        final_text: list[str] = []
        for message in runner:
            for block in message.content:
                if block.type == "text":
                    final_text.append(block.text)
        return "\n\n".join(final_text)
