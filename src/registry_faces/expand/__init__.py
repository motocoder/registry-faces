"""registry-faces' binding onto the shared ``web_scrubber.expand`` engine.

Country-keyed adapter-registry shape (see ``web_scrubber.expand.country_kit``):
one generated sex-offender-registry adapter per covered country, no git layer.
Supplies only the domain bits — adapter package, name suffix, research domain, and
the scout prompt (``prompts.py``).
"""
from __future__ import annotations

from pathlib import Path

from web_scrubber.expand import (
    ExpandSpec,
    PolicyConfig,
    already_supervised,
    run_expand as _run_expand,
    supervise_self,
)
from web_scrubber.expand.country_kit import (
    country_ledger,
    country_scope,
    ensure_country_ledger,
    iso_adapter_name,
    known_adapter_names,
    load_adapter_verify,
    remove_adapter,
)

from . import prompts

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LEDGER_PATH = PROJECT_ROOT / "docs" / "country_coverage.csv"
ADAPTERS_OUT = PROJECT_ROOT / "adapters_generated"
ADAPTER_PACKAGE = "registry_faces.adapters"
ENV_PREFIX = "REGISTRY_FACES"
ADAPTER_SUFFIX = "registry"
RESEARCH_DOMAIN = "national public sex-offender registry"
PAUSE_FILE = Path.home() / ".web-scrubber" / "expand-registry-faces.pause"


def _ledger():
    return country_ledger(LEDGER_PATH)


def make_source_code(t) -> str:
    return iso_adapter_name(t.key[0], ADAPTER_SUFFIX, known_adapter_names(_ledger()))


def verify(t, code):
    return load_adapter_verify(code, ADAPTER_PACKAGE, ADAPTERS_OUT, cwd=PROJECT_ROOT)


def _discard(t, code) -> None:
    remove_adapter(ADAPTERS_OUT, code)


def research_domain(settings) -> str:
    return RESEARCH_DOMAIN


def _build_prompt(t, code, theory=None) -> str:
    return prompts.build_prompt(t, code, sorted(known_adapter_names(_ledger())), theory)


def _system_prompt():
    try:
        from ..agent.system_prompt import SYSTEM_PROMPT

        return SYSTEM_PROMPT
    except Exception:  # noqa: BLE001
        return None


def build_scope(args) -> dict[str, list[str]]:
    return country_scope(args, PROJECT_ROOT)


def build_spec() -> ExpandSpec:
    return ExpandSpec(
        task="registry-faces",
        project_root=PROJECT_ROOT,
        data_dir=PROJECT_ROOT,
        ledger=_ledger(),
        build_prompt=_build_prompt,
        verify_fn=verify,
        make_source_code=make_source_code,
        env_prefix=ENV_PREFIX,
        system_prompt=_system_prompt(),
        git=None,
        on_discard=_discard,
        research_domain_fn=research_domain,
        policy=PolicyConfig(thin_ceiling=1),
    )


def run_expand(args) -> int:
    ensure_country_ledger(LEDGER_PATH)
    if getattr(args, "supervise", False) and not already_supervised():
        return supervise_self(max_hours=getattr(args, "max_hours", None) or 0.0, pause_file=PAUSE_FILE)
    args.scope = build_scope(args)
    return _run_expand(build_spec(), args)


__all__ = ["run_expand", "build_spec", "build_scope", "make_source_code", "verify", "research_domain"]
