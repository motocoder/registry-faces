"""Per-country coverage ledger for the sex-offender registry discovery loop.

``docs/country_coverage.csv`` records, for every ISO-3166-1 country, whether we
have a working wanted/fugitive adapter (``covered``), have confirmed there is no
public enumerable source (``unsupported`` — the "doesn't allow this" record the
loop is meant to remember), or have not yet looked (``searched`` = the work
queue). The work unit is one country.

Columns: ``Country, CountryName, Status, Adapter, SourceURL, Notes``.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from web_scrubber.countries import COUNTRIES, country_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LEDGER = PROJECT_ROOT / "docs" / "country_coverage.csv"
CURSOR = PROJECT_ROOT / ".expand" / "country_cursor.json"
FIELDS = ["Country", "CountryName", "Status", "Adapter", "SourceURL", "Notes"]

STATUS_TODO = "searched"
STATUS_COVERED = "covered"
STATUS_UNSUPPORTED = "unsupported"


@dataclass
class Target:
    country: str          # ISO alpha-2
    name: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.country})"


def ensure_ledger() -> None:
    """Create + seed the ledger with every ISO country (status=searched) if absent."""
    if LEDGER.exists():
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for code, name in COUNTRIES:
            w.writerow({"Country": code, "CountryName": name, "Status": STATUS_TODO,
                        "Adapter": "", "SourceURL": "", "Notes": ""})


def _rows() -> list[dict]:
    ensure_ledger()
    with LEDGER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(rows: list[dict]) -> None:
    with LEDGER.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in FIELDS} for r in rows)


def _status(cc: str) -> set[str]:
    return {r["Country"].upper() for r in _rows()}


def update_row(cc: str, *, status: str, adapter: str = "", source_url: str = "",
               notes: str = "") -> None:
    """Set the ledger row for ``cc`` (create it if somehow missing)."""
    rows = _rows()
    cc = cc.upper()
    found = False
    for r in rows:
        if r["Country"].upper() == cc:
            r["Status"] = status
            if adapter:
                r["Adapter"] = adapter
            if source_url:
                r["SourceURL"] = source_url
            r["Notes"] = notes[:200]
            found = True
            break
    if not found:
        rows.append({"Country": cc, "CountryName": country_name(cc), "Status": status,
                     "Adapter": adapter, "SourceURL": source_url, "Notes": notes[:200]})
    _write(rows)


def mark_covered(cc: str, adapter: str, source_url: str) -> None:
    update_row(cc, status=STATUS_COVERED, adapter=adapter, source_url=source_url, notes="")


def mark_unsupported(cc: str, reason: str) -> None:
    update_row(cc, status=STATUS_UNSUPPORTED, notes=reason)


def _codes_with(status: str) -> set[str]:
    return {r["Country"].upper() for r in _rows() if r.get("Status") == status}


def covered_count() -> int:
    return len(_codes_with(STATUS_COVERED))


def unsupported_count() -> int:
    return len(_codes_with(STATUS_UNSUPPORTED))


def next_country(only: list[str] | None = None) -> Target | None:
    """Next never-attempted country (status=searched), round-robin after the cursor.
    ``only`` optionally restricts to a subset of ISO codes."""
    rows = _rows()
    pool = [r for r in rows if r.get("Status") == STATUS_TODO]
    if only:
        want = {c.upper() for c in only}
        pool = [r for r in pool if r["Country"].upper() in want]
    if not pool:
        return None
    codes = [r["Country"].upper() for r in pool]
    last = _load_cursor()
    start = 0
    if last in codes:
        start = (codes.index(last) + 1) % len(codes)
    r = pool[start]
    return Target(country=r["Country"].upper(), name=r.get("CountryName") or country_name(r["Country"]))


def failed_countries(only: list[str] | None = None) -> list[str]:
    """Unsupported countries — reconsidered by the research phase."""
    codes = sorted(_codes_with(STATUS_UNSUPPORTED))
    if only:
        want = {c.upper() for c in only}
        codes = [c for c in codes if c in want]
    return codes


def failure_reasons(codes: list[str]) -> list[dict]:
    want = {c.upper() for c in codes}
    return [{"target": r["Country"], "reason": r.get("Notes", "")}
            for r in _rows()
            if r["Country"].upper() in want and r.get("Status") == STATUS_UNSUPPORTED]


def known_adapter_names() -> set[str]:
    return {r["Adapter"] for r in _rows() if r.get("Adapter")}


def _load_cursor() -> str | None:
    try:
        return json.loads(CURSOR.read_text(encoding="utf-8")).get("country")
    except (OSError, ValueError):
        return None


def save_cursor(t: Target) -> None:
    CURSOR.parent.mkdir(parents=True, exist_ok=True)
    CURSOR.write_text(json.dumps({"country": t.country}), encoding="utf-8")


def suggested_adapter_name(cc: str) -> str:
    """Deterministic base module name, e.g. 'NG' -> 'ng_registry'; dedup with a suffix."""
    taken = {n.lower() for n in known_adapter_names()}
    base = f"{cc.lower()}_registry"
    if base not in taken:
        return base
    for n in range(2, 99):
        cand = f"{cc.lower()}_registry{n}"
        if cand not in taken:
            return cand
    return base


__all__ = [
    "Target", "LEDGER", "ensure_ledger", "update_row", "mark_covered", "mark_unsupported",
    "next_country", "failed_countries", "failure_reasons", "covered_count",
    "unsupported_count", "save_cursor", "suggested_adapter_name", "known_adapter_names",
    "STATUS_TODO", "STATUS_COVERED", "STATUS_UNSUPPORTED",
]
