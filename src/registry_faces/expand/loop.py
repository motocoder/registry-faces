"""Continuous per-country sex-offender registry discovery loop.

One iteration: pick the next uncovered country -> ask the agent to research a
public enumerable fugitive source and write an adapter -> verify it yields
records -> record covered / unsupported in docs/country_coverage.csv. Runs until
paused (control-panel sentinel / Ctrl-C), --once, or the daily quota is hit.

Local mode (no git): generated adapters + the ledger are written to the working
tree; commit them when you're happy. Mirrors crime-crawler's expand loop minus
the multi-machine git sync.
"""
from __future__ import annotations

import os
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import coverage
from .coverage import Target
from .prompts import build_prompt

try:
    from web_scrubber.progress import emit_progress as _emit
except Exception:  # pragma: no cover
    def _emit(task, status, *, region="", message="", counts=None, file=None):
        print(f"[{task}] {status} {region} {message} {counts or ''}".rstrip(), flush=True)

try:
    from web_scrubber.agent.cli_agent import run_cli_agent
except Exception:  # pragma: no cover
    run_cli_agent = None  # type: ignore

try:
    from web_scrubber.agent import research
except Exception:  # pragma: no cover
    research = None  # type: ignore

TASK = "registry-faces"
ADAPTERS_OUT = coverage.PROJECT_ROOT / "adapters_generated"
ADAPTER_PACKAGE = "registry_faces.adapters"
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "WebSearch", "WebFetch", "Glob", "Grep"]
QUOTA = coverage.PROJECT_ROOT / ".expand" / "country_quota.json"
RESEARCH_LOG = coverage.PROJECT_ROOT / ".expand" / "research_log.json"
ENV_PREFIX = "REGISTRY_FACES"


@dataclass
class Settings:
    countries: list[str] = field(default_factory=list)
    engine: str = "claude"
    model: str | None = None
    delay_seconds: float = 10.0
    max_per_day: int = 0
    agent_timeout: int = 1800
    research_idle_seconds: float = 3600.0


def load_settings(args) -> Settings:
    data: dict = {}
    if getattr(args, "regions", None):
        p = Path(args.regions)
        if not p.is_absolute():
            p = coverage.PROJECT_ROOT / p
        if p.is_file():
            with p.open("rb") as f:
                data = tomllib.load(f)
    eng = data.get("engine", {}) if isinstance(data.get("engine"), dict) else {}
    pace = data.get("pacing", {}) if isinstance(data.get("pacing"), dict) else {}
    s = Settings(
        countries=list(data.get("countries", []) or []),
        engine=args.engine or eng.get("name", "claude"),
        model=args.model or (eng.get("model") or None),
        delay_seconds=float(pace.get("delay_seconds", 10.0)),
        max_per_day=int(pace.get("max_per_day", 0)),
        agent_timeout=int(pace.get("agent_timeout", 1800)),
        research_idle_seconds=float(pace.get("research_idle_seconds", 3600.0)),
    )
    if getattr(args, "country", None):
        s.countries = [args.country]
    if getattr(args, "delay", None) is not None:
        s.delay_seconds = args.delay
    if getattr(args, "max_per_day", None) is not None:
        s.max_per_day = args.max_per_day
    return s


def _paused() -> bool:
    pf = os.environ.get("WEB_SCRUBBER_PAUSE_FILE")
    return bool(pf and os.path.exists(pf))


def _sleep_interruptible(seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _paused():
            return True
        time.sleep(min(0.5, max(0.0, end - time.monotonic())))
    return _paused()


def _quota_used_today() -> int:
    try:
        import json
        return int(json.loads(QUOTA.read_text(encoding="utf-8")).get(date.today().isoformat(), 0))
    except (OSError, ValueError):
        return 0


def _bump_quota() -> None:
    import json
    today = date.today().isoformat()
    QUOTA.parent.mkdir(parents=True, exist_ok=True)
    QUOTA.write_text(json.dumps({today: _quota_used_today() + 1}), encoding="utf-8")


def _registered() -> list[str]:
    return sorted(coverage.known_adapter_names())


def _verify(name: str, max_records: int = 5, max_seconds: float = 120.0) -> int:
    """Load + run the generated adapter in a SUBPROCESS with a hard timeout, and
    count records (returns -1 on any error or timeout = unverified).

    A subprocess is the only reliable wall-clock cap: a built-but-broken adapter
    (e.g. a slow name-pair brute-forcer that yields nothing) would otherwise block
    the loop for many minutes until it exhausts its whole search. ``for _ in a.run()``
    is arity-agnostic (handles 2- and 3-tuple person-keyed adapters)."""
    import subprocess
    import sys

    code = (
        "from web_scrubber.discovery import load_adapter\n"
        f"a = load_adapter({name!r}, {ADAPTER_PACKAGE!r}, {str(ADAPTERS_OUT)!r})\n"
        "n = 0\n"
        "for _ in a.run():\n"
        "    n += 1\n"
        f"    if n >= {max_records}:\n        break\n"
        "print(n)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(coverage.PROJECT_ROOT),
            capture_output=True, text=True, timeout=max_seconds,
        )
    except subprocess.TimeoutExpired:
        return -1
    out = (proc.stdout or "").strip().splitlines()
    try:
        return int(out[-1]) if out else -1
    except ValueError:
        return -1


def _remove_adapter(name: str) -> None:
    p = ADAPTERS_OUT / f"{name}.py"
    try:
        p.unlink()
    except OSError:
        pass


def _system_prompt() -> str | None:
    try:
        from ..agent.system_prompt import SYSTEM_PROMPT
        return SYSTEM_PROMPT
    except Exception:  # noqa: BLE001
        return None


def run_expand(args) -> int:
    s = load_settings(args)
    coverage.ensure_ledger()
    if run_cli_agent is None:
        _emit(TASK, "error", message="web_scrubber.agent.cli_agent unavailable")
        return 1
    dry = getattr(args, "dry_run", False)
    once = getattr(args, "once", False)
    _emit(TASK, "starting",
          message=f"engine={s.engine} countries={s.countries or 'ALL'} "
                  f"covered={coverage.covered_count()} unsupported={coverage.unsupported_count()}")
    try:
        while True:
            if _paused():
                _emit(TASK, "paused", message="pause sentinel present")
                return 0
            if s.max_per_day and _quota_used_today() >= s.max_per_day:
                _emit(TASK, "idle", message=f"daily quota reached ({s.max_per_day})")
                return 0

            t = coverage.next_country(s.countries)
            if t is None:
                outcome = _research_phase(s, args)
                if outcome == "applied":
                    continue
                if outcome == "none":
                    _emit(TASK, "idle", message="no uncovered countries; nothing to research")
                    return 0
                if once:
                    _emit(TASK, "idle", message="research dry (--once)")
                    return 0
                if _sleep_interruptible(s.research_idle_seconds):
                    _emit(TASK, "paused", message="pause sentinel present")
                    return 0
                continue

            coverage.save_cursor(t)
            name = coverage.suggested_adapter_name(t.country)
            _emit(TASK, "scouting", region=t.label, message=f"adapter={name}")
            result = run_cli_agent(
                prompt=build_prompt(t, name, _registered()),
                cwd=coverage.PROJECT_ROOT, engine=s.engine, model=s.model,
                allowed_tools=ALLOWED_TOOLS, timeout=s.agent_timeout,
                env_prefix=ENV_PREFIX, system=_system_prompt(),
            )
            _handle_result(t, name, result, dry)

            if once:
                return 0
            if _sleep_interruptible(s.delay_seconds):
                _emit(TASK, "paused", message="pause sentinel present")
                return 0
    except KeyboardInterrupt:  # pragma: no cover
        _emit(TASK, "stopped", message="interrupted")
        return 130


def _handle_result(t: Target, name: str, result, dry: bool) -> None:
    if result is None or not getattr(result, "ok", False):
        _remove_adapter(name)
        msg = getattr(result, "error", "agent error") if result else "agent returned nothing"
        _emit(TASK, "skipped", region=t.label, message=f"agent: {str(msg)[:120]}")
        return
    payload = result.payload or {}
    outcome = str(payload.get("outcome", "")).lower()
    reason = str(payload.get("reason", "")) or "unsupported"
    source_url = str(payload.get("source_url", ""))

    if outcome == "unsupported":
        _remove_adapter(name)
        if not dry:
            coverage.mark_unsupported(t.country, reason)
        _emit(TASK, "unsupported", region=t.label, message=reason[:120])
        return

    _emit(TASK, "verifying", region=t.label, message=f"adapter={name}")
    n = _verify(name)
    if n >= 1:
        if dry:
            _emit(TASK, "verified", region=t.label, message=f"{name}: {n} records (dry-run)")
            return
        coverage.mark_covered(t.country, name, source_url)
        _bump_quota()
        _emit(TASK, "covered", region=t.label, message=f"{name}: {n} records",
              counts={"covered": coverage.covered_count()})
    else:
        _remove_adapter(name)
        if not dry:
            coverage.mark_unsupported(t.country, f"verify failed (records={n})")
        _emit(TASK, "skipped", region=t.label, message=f"verify failed (records={n})")


def _research_phase(s: Settings, args) -> str:
    if research is None:
        return "none"
    failed = coverage.failed_countries(s.countries)
    if not failed:
        return "none"
    _emit(TASK, "researching", message=f"{len(failed)} unsupported; seeking new approaches")
    theories = research.propose_theories(
        failures=coverage.failure_reasons(failed),
        domain="national public sex-offender registry",
        cwd=coverage.PROJECT_ROOT, log_path=RESEARCH_LOG,
        engine=s.engine, model=s.model, env_prefix=ENV_PREFIX, timeout=s.agent_timeout,
    )
    if not theories:
        return "dry"
    research.record_tried(RESEARCH_LOG, theories)
    hint = " | ".join(t.approach for t in theories)
    for cc in failed:
        if _paused():
            return "applied"
        tgt = Target(country=cc, name=coverage.country_name(cc) if hasattr(coverage, "country_name") else cc)
        coverage.save_cursor(tgt)
        name = coverage.suggested_adapter_name(cc)
        _emit(TASK, "scouting", region=tgt.label, message=f"[research retry] adapter={name}")
        result = run_cli_agent(
            prompt=build_prompt(tgt, name, _registered(), theory=hint),
            cwd=coverage.PROJECT_ROOT, engine=s.engine, model=s.model,
            allowed_tools=ALLOWED_TOOLS, timeout=s.agent_timeout,
            env_prefix=ENV_PREFIX, system=_system_prompt(),
        )
        _handle_result(tgt, name, result, getattr(args, "dry_run", False))
        if _sleep_interruptible(s.delay_seconds):
            return "applied"
    return "applied"


__all__ = ["run_expand", "Settings", "load_settings"]
