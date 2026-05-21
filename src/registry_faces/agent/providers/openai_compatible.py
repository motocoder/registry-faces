"""OpenAI-compatible provider.

Covers OpenAI itself plus every service that speaks the OpenAI Chat Completions
API: Ollama (local), Groq, Cerebras, OpenRouter, GitHub Models. Differences
are just `base_url` + `api_key` + `model`.

Runs the tool loop manually since not every backend supports the Assistants
API or auto-loop helpers consistently.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ..tool_schema import tool_to_openai_schema
from .base import Provider

try:
    from openai import OpenAI

    _AVAILABLE = True
    _IMPORT_ERROR: Exception | None = None
except ImportError as e:
    _AVAILABLE = False
    _IMPORT_ERROR = e


class OpenAICompatibleProvider(Provider):
    name = "openai-compatible"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not _AVAILABLE:
            raise RuntimeError(
                "openai SDK not installed. Run: pip install 'registry-faces[openai]'"
            ) from _IMPORT_ERROR
        self.model = model
        kwargs: dict = {"api_key": api_key or "no-key"}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)

    def run_agent(
        self,
        *,
        system: str,
        user_prompt: str,
        tools: list[Callable],
        max_iterations: int = 15,
    ) -> str:
        tools_by_name = {fn.__name__: fn for fn in tools}
        schemas = [tool_to_openai_schema(fn) for fn in tools]

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]

        for _ in range(max_iterations):
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=schemas,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            assistant_entry: dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_entry)

            if not msg.tool_calls:
                return msg.content or ""

            for tc in msg.tool_calls:
                fn = tools_by_name.get(tc.function.name)
                if fn is None:
                    result: object = f"ERROR: unknown tool {tc.function.name}"
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        result = fn(**args)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                content = result if isinstance(result, str) else json.dumps(result, default=str)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    }
                )

        return "Hit max iterations without completion."
