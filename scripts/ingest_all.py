"""Ingest every non-blacklisted state adapter, with per-state health grading.

The NSOPW adapters depend on a live, Cloudflare-gated federal API. When that
source changes shape, blocks us, or goes dark, a plain ``ingest`` loop would
silently write an empty or malformed state and you'd only notice months later.
This runner grades each state so breakage surfaces immediately.

Grading per state:
  FAIL  - adapter failed to import/build, raised mid-run, every query errored,
          or it produced 0 records (endpoint/API likely changed)
  WARN  - query failure-rate over --max-fail-rate (Cloudflare throttling /
          partial outage), or UNKNOWN-name rate over --max-unknown-rate
          (the payload shape drifted and normalize() no longer finds names)
  OK    - records ingested, failure/unknown rates within thresholds

Exit code = number of FAIL states (0 = all healthy), so a scheduler or CI step
can alert on nonzero. A JSON health report is written under
``registry-runs/ingest_all/<timestamp>/health.json`` for the audit trail.

Usage:
  .venv/Scripts/python.exe scripts/ingest_all.py                  # full ingest
  .venv/Scripts/python.exe scripts/ingest_all.py --sample         # quick health pass
  .venv/Scripts/python.exe scripts/ingest_all.py --only US-TX US-WI
  .venv/Scripts/python.exe scripts/ingest_all.py --max-fail-rate 0.6
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from registry_faces import adapters as _adapters_pkg
from registry_faces.blacklist import BLACKLIST
from registry_faces.store import FileStore

# Health verdicts.
OK, WARN, FAIL = "OK", "WARN", "FAIL"


def discover_adapters() -> list[tuple[str, str, object]]:
    """Return (module_name, jurisdiction, build_fn) for every runnable, non-
    blacklisted in-package state adapter. Build/import errors are surfaced as
    FAIL rows by the caller, not swallowed here."""
    out: list[tuple[str, str, object]] = []
    for mi in pkgutil.iter_modules(_adapters_pkg.__path__):
        if mi.name == "base" or mi.name.startswith("_"):
            continue
        out.append((mi.name, None, None))  # resolved in run loop so import errors grade as FAIL
    return out


def grade(records: int, ok: int, fail: int, unknown: int,
          max_fail_rate: float, max_unknown_rate: float) -> tuple[str, list[str]]:
    """Turn raw counts into a verdict + the reasons behind it."""
    reasons: list[str] = []
    total_q = ok + fail
    fail_rate = (fail / total_q) if total_q else 0.0
    unknown_rate = (unknown / records) if records else 0.0

    if records == 0:
        if total_q and ok == 0:
            return FAIL, ["every query errored — endpoint/Cloudflare likely broke"]
        return FAIL, ["0 records produced — source returned nothing or shape changed"]

    if ok == 0 and fail > 0:
        reasons.append("no successful queries despite records (suspicious)")
    if fail_rate > max_fail_rate:
        reasons.append(f"query failure-rate {fail_rate:.0%} > {max_fail_rate:.0%}")
    if unknown_rate > max_unknown_rate:
        reasons.append(f"UNKNOWN-name rate {unknown_rate:.0%} > {max_unknown_rate:.0%} "
                       f"— normalize() may not match the payload")

    return (WARN if reasons else OK), reasons


def run_state(module_name: str, store: FileStore, *, sample: bool,
              sample_zips: int, cap: int) -> dict:
    """Ingest one state. Never raises — failures become a FAIL row."""
    row: dict = {"module": module_name, "jurisdiction": None, "status": FAIL,
                 "records": 0, "ok": 0, "fail": 0, "unknown": 0,
                 "sample_name": None, "reasons": [], "error": None}
    try:
        mod = importlib.import_module(f"registry_faces.adapters.{module_name}")
        adapter = mod.build()
    except Exception:
        row["error"] = traceback.format_exc(limit=3).strip()
        row["reasons"] = ["import/build failed"]
        return row

    row["jurisdiction"] = getattr(adapter, "jurisdiction", None)
    if row["jurisdiction"] in BLACKLIST:
        row["status"] = "SKIP"
        row["reasons"] = ["blacklisted"]
        return row

    if sample:
        zr = getattr(adapter, "zip_range", range(0, 0))
        if zr.stop - zr.start > sample_zips:
            adapter.zip_range = range(zr.start, zr.start + sample_zips)
        adapter.name_sweep = False

    records = unknown = 0
    sample_name = None
    try:
        for record, photo_refs in adapter.run():
            store.upsert(record, photos=photo_refs)
            records += 1
            if record.identity.full_name == "UNKNOWN":
                unknown += 1
            elif sample_name is None:
                sample_name = record.identity.full_name
            if sample and records >= cap:
                break
    except Exception:
        row["error"] = traceback.format_exc(limit=4).strip()
        row["records"] = records
        row["ok"] = getattr(adapter, "_n_success", 0)
        row["fail"] = getattr(adapter, "_n_failed", 0)
        row["reasons"] = ["raised mid-run"]
        return row

    ok = getattr(adapter, "_n_success", 0)
    fail = getattr(adapter, "_n_failed", 0)
    status, reasons = grade(records, ok, fail, unknown, MAX_FAIL_RATE, MAX_UNKNOWN_RATE)
    row.update(records=records, ok=ok, fail=fail, unknown=unknown,
               sample_name=sample_name, status=status, reasons=reasons)
    return row


def main(argv: list[str] | None = None) -> int:
    global MAX_FAIL_RATE, MAX_UNKNOWN_RATE
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", default="registry", type=Path)
    parser.add_argument("--sample", action="store_true",
                        help="Bounded health pass: truncate each ZIP range, skip the "
                             "name sweep, and cap records per state.")
    parser.add_argument("--sample-zips", type=int, default=120,
                        help="In --sample mode, ZIPs to probe per state (default 120).")
    parser.add_argument("--cap", type=int, default=50,
                        help="In --sample mode, max records per state (default 50).")
    parser.add_argument("--only", nargs="*", default=None,
                        help="Restrict to these jurisdiction codes, e.g. US-TX US-WI.")
    parser.add_argument("--max-fail-rate", type=float, default=0.5)
    parser.add_argument("--max-unknown-rate", type=float, default=0.5)
    args = parser.parse_args(argv)
    MAX_FAIL_RATE = args.max_fail_rate
    MAX_UNKNOWN_RATE = args.max_unknown_rate

    only = {c.upper() for c in args.only} if args.only else None
    modules = [m for (m, _, _) in discover_adapters()]
    print(f"ingest_all: {len(modules)} adapters discovered, "
          f"{'SAMPLE' if args.sample else 'FULL'} mode, registry={args.registry.resolve()}")
    if only:
        print(f"  restricted to: {sorted(only)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = Path("registry-runs") / "ingest_all" / stamp
    report_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    store = FileStore(args.registry)
    try:
        for i, module_name in enumerate(modules, 1):
            print(f"\n[{i}/{len(modules)}] {module_name} ...", flush=True)
            # Resolve jurisdiction cheaply to honour --only / blacklist before running.
            try:
                mod = importlib.import_module(f"registry_faces.adapters.{module_name}")
                jur = mod.build().jurisdiction
            except Exception:
                jur = None
            if only is not None and (jur or "").upper() not in only:
                continue
            if jur in BLACKLIST:
                print(f"  SKIP {jur} (blacklisted)")
                continue

            row = run_state(module_name, store, sample=args.sample,
                            sample_zips=args.sample_zips, cap=args.cap)
            rows.append(row)
            store.close()  # checkpoint index+manifest after each state
            tag = {"OK": "OK  ", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}[row["status"]]
            extra = f" — {'; '.join(row['reasons'])}" if row["reasons"] else ""
            print(f"  {tag} {row['jurisdiction']}: {row['records']} records "
                  f"(ok={row['ok']} fail={row['fail']}){extra}", flush=True)
    finally:
        store.close()

    # ---- summary -------------------------------------------------------
    by_status: dict[str, list[dict]] = {OK: [], WARN: [], FAIL: []}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)

    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(by_status[OK])} OK, {len(by_status[WARN])} WARN, "
          f"{len(by_status[FAIL])} FAIL  ({sum(r['records'] for r in rows)} records total)")
    for status in (FAIL, WARN):
        for r in by_status[status]:
            print(f"  {status} {r['jurisdiction']}: {'; '.join(r['reasons']) or r['error'] or '?'}")

    report = {"generated_at": stamp, "mode": "sample" if args.sample else "full",
              "thresholds": {"max_fail_rate": MAX_FAIL_RATE, "max_unknown_rate": MAX_UNKNOWN_RATE},
              "summary": {s: len(by_status[s]) for s in (OK, WARN, FAIL)}, "states": rows}
    (report_dir / "health.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nhealth report -> {report_dir / 'health.json'}")

    return len(by_status[FAIL])


if __name__ == "__main__":
    sys.exit(main())
