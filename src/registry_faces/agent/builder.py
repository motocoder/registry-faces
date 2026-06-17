"""Adapter-building agent — registry-faces binding over the framework builder.

Preserves the historical registry-faces signatures (``build_adapter_from_url``
and two-arg ``resolve_mode``) so the CLI and desktop workers are unchanged.
"""

from __future__ import annotations

from typing import Literal

from web_scrubber.agent.builder import build_adapter_from_url as _build
from web_scrubber.agent.builder import resolve_mode as _resolve_mode

from .system_prompt import SYSTEM_PROMPT
from .tools import CTX

Mode = Literal["auto", "create", "update"]


def resolve_mode(mode: Mode, adapter_name: str) -> Literal["create", "update"]:
    """Resolve ``auto`` against whether the generated adapter file exists."""
    return _resolve_mode(mode, adapter_name, CTX.adapters_out)


def build_adapter_from_url(
    url: str,
    adapter_name: str,
    jurisdiction: str,
    provider: str | None = None,
    model: str | None = None,
    mode: Mode = "auto",
) -> str:
    """Run the agent against a URL. Writes ``adapters_generated/<adapter_name>.py``."""
    return _build(
        url=url,
        adapter_name=adapter_name,
        jurisdiction=jurisdiction,
        ctx=CTX,
        system_prompt=SYSTEM_PROMPT,
        provider=provider,
        model=model,
        mode=mode,
        env_prefix="REGISTRY_FACES",
        record_noun="offender",
    )
