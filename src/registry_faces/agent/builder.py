"""Adapter-building agent — takes a source URL, returns a working adapter.

Two modes:

  * **create** — write a new adapter from scratch.
  * **update** — review and minimally edit an existing adapter.

Default `mode="auto"` picks based on whether `adapters_generated/<name>.py`
already exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from .providers import get_provider
from .system_prompt import SYSTEM_PROMPT
from .tools import ADAPTERS_OUT, TOOLS

Mode = Literal["auto", "create", "update"]


def _resolve_mode(mode: Mode, adapter_name: str) -> Literal["create", "update"]:
    if mode == "create":
        return "create"
    if mode == "update":
        return "update"
    # auto
    existing = ADAPTERS_OUT / f"{adapter_name}.py"
    return "update" if existing.exists() else "create"


def _user_prompt(mode: Literal["create", "update"], url: str, name: str, jurisdiction: str) -> str:
    if mode == "update":
        return (
            f"An adapter named {name!r} already exists at "
            f"adapters_generated/{name}.py. Your job: review it, test it "
            f"against the live source, and make minimal targeted changes to "
            f"fix anything broken. Do NOT rewrite from scratch unless the "
            f"source schema has fundamentally changed.\n\n"
            f"Workflow:\n"
            f"  1. read_existing_adapter({name!r}) — see the current code.\n"
            f"  2. test_adapter({name!r}) — does it still produce valid records?\n"
            f"  3. If it works, report 'no changes needed' and stop.\n"
            f"  4. If it fails, fetch_url against the source to see what "
            f"changed, then write back the updated adapter with the "
            f"smallest diff that fixes it.\n\n"
            f"  URL: {url}\n"
            f"  Adapter name: {name}\n"
            f"  Jurisdiction code: {jurisdiction}"
        )
    # create
    return (
        f"Build a new adapter that ingests offender records from this source "
        f"into the canonical schema.\n\n"
        f"  URL: {url}\n"
        f"  Adapter name: {name}\n"
        f"  Jurisdiction code: {jurisdiction}\n\n"
        f"Investigate, propose a field mapping, write the adapter, then test it."
    )


def build_adapter_from_url(
    url: str,
    adapter_name: str,
    jurisdiction: str,
    provider: str | None = None,
    model: str | None = None,
    mode: Mode = "auto",
) -> str:
    """Run the agent against a URL. Writes adapters_generated/<adapter_name>.py.

    Args:
        url: Source URL the agent investigates.
        adapter_name: Name for the adapter module (lowercase + underscores).
        jurisdiction: Jurisdiction code like "US-FL".
        provider: Provider preset name. Defaults to env var or "anthropic".
        model: Override the preset's default model.
        mode: "auto" (default), "create", or "update". "auto" picks based on
              whether the adapter file already exists.

    Returns:
        The agent's final text report.
    """
    resolved_mode = _resolve_mode(mode, adapter_name)
    p = get_provider(preset=provider, model=model)
    user_prompt = _user_prompt(resolved_mode, url, adapter_name, jurisdiction)
    return p.run_agent(system=SYSTEM_PROMPT, user_prompt=user_prompt, tools=TOOLS)


def resolve_mode(mode: Mode, adapter_name: str) -> Literal["create", "update"]:
    """Public helper for the CLI to report which mode auto resolved to."""
    return _resolve_mode(mode, adapter_name)
