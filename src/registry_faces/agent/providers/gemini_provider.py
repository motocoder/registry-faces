"""Google Gemini provider.

Uses the `google-genai` SDK's automatic function calling — the SDK derives
schemas from Python type hints + docstrings and handles the agent loop itself.

Adds 429 backoff: the free tier rate-limits per minute, and an agent that
makes many tool calls in quick succession can exhaust the budget mid-session.
On 429 we parse the `retryDelay` from the error envelope and sleep, up to
`max_retries` attempts. Since `automatic_function_calling` runs the whole
loop server-side, a 429 mid-loop loses state — restart costs tokens but is
the only option short of writing our own loop.

Best free option among hosted models: long context window is useful when the
agent inspects large HTML samples.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from .base import Provider

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    _AVAILABLE = True
    _IMPORT_ERROR: Exception | None = None
except ImportError as e:
    _AVAILABLE = False
    _IMPORT_ERROR = e


def _parse_retry_seconds(err: "genai_errors.ClientError", default: float = 30.0) -> float:
    """Pull the server-suggested retryDelay out of the error response."""
    try:
        details = (err.details or {}).get("error", {}).get("details", []) or []
    except Exception:
        details = []
    for d in details:
        if isinstance(d, dict) and d.get("@type", "").endswith("RetryInfo"):
            delay = str(d.get("retryDelay", "")).rstrip("s")
            try:
                return float(delay)
            except ValueError:
                pass
    return default


def _is_daily_quota_exhausted(err: "genai_errors.ClientError") -> bool:
    """Detect when 429 is a per-day quota hit. Retrying won't help until UTC midnight."""
    try:
        details = (err.details or {}).get("error", {}).get("details", []) or []
    except Exception:
        return False
    for d in details:
        if not isinstance(d, dict):
            continue
        if not d.get("@type", "").endswith("QuotaFailure"):
            continue
        for v in d.get("violations", []) or []:
            if "PerDay" in str(v.get("quotaId", "")):
                return True
    return False


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model: str, api_key: str | None = None) -> None:
        if not _AVAILABLE:
            raise RuntimeError(
                "google-genai SDK not installed. Run: pip install 'registry-faces[gemini]'"
            ) from _IMPORT_ERROR
        self.model = model
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def _generate(
        self,
        system: str,
        user_prompt: str,
        tools: list[Callable],
        max_iterations: int,
    ):
        return self.client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system,
                tools=tools,
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=max_iterations,
                ),
            ),
        )

    def run_agent(
        self,
        *,
        system: str,
        user_prompt: str,
        tools: list[Callable],
        max_iterations: int = 15,
        max_retries: int = 6,
    ) -> str:
        attempt = 0
        while True:
            try:
                response = self._generate(system, user_prompt, tools, max_iterations)
                return response.text or ""
            except genai_errors.ClientError as e:
                # 429 = quota exhausted; back off and retry per the server's hint —
                # unless it's a per-day quota, in which case waiting won't help.
                if getattr(e, "code", None) != 429:
                    raise
                if _is_daily_quota_exhausted(e):
                    raise RuntimeError(
                        f"Gemini daily free-tier quota exhausted for model {self.model!r}. "
                        f"Resets at UTC midnight. Switch to a different model or provider, "
                        f"or wait."
                    ) from e
                if attempt >= max_retries:
                    raise
                delay = max(_parse_retry_seconds(e), 5.0) + 1.0
                print(
                    f"  Gemini rate-limited; sleeping {delay:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                attempt += 1
