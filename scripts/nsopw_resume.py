"""Generic resume / retry runner for NSOPW-based state adapters.

The WARN states (TX/VT/WI, ...) finish a full scrape but with a high
per-query failure rate — Cloudflare rate-limits the NSOPW API at the 6s
default pace, so a chunk of zip-batches / name-pairs come back as
``fetch_threw`` / ``api_sc_*`` (retryable). The data those queries would
have returned is simply missing, which is why their coverage is partial.

This re-runs only the *pending* queries — never-attempted planned queries
plus retryable failures from the latest run — at a slower, CF-friendly
pace, merging new records into the same FileStore (idempotent upsert). The
adapter itself does the logging and accepts a ``queries=`` subset; this
just computes the subset and drives it. Generalizes ``wa_resume.py`` to any
``NsopwAdapter`` subclass.

Usage:
    python scripts/nsopw_resume.py texas vermont wisconsin
    python scripts/nsopw_resume.py texas --dry-run        # size the work only
    python scripts/nsopw_resume.py texas --delay 8 --backoff 30

Pending = (planned_enumeration - already_succeeded) + retryable_failures,
mirroring wa_resume. ``planned_enumeration`` = zip-batches + name-pairs in
the adapter's own order. api_117 (zip simply doesn't exist) is never
retried.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from registry_faces.adapters._nsopw import BATCH_SIZE, NAME_LETTERS
from registry_faces.cli import _load_adapter
from registry_faces.store import FileStore

REGISTRY_ROOT = Path("registry")
LOG_ROOT = Path("registry-runs")


def _canonical(query: dict) -> tuple:
    """Stable dedup key. Ignores clientIp (always empty/server-set). Matches
    the query bodies the NSOPW base emits: zip batches/singletons and name
    pairs (incl. 511 surname extensions)."""
    if "zips" in query:
        return ("zip", tuple(sorted(query["zips"])))
    if "firstName" in query:
        return ("name", query.get("firstName", ""), query.get("lastName", ""))
    return ("unknown", json.dumps(query, sort_keys=True))


def _enumerate_planned(zip_range: range, batch_size: int, code: str, name_sweep: bool) -> list[dict]:
    """Reproduce the full set of queries the adapter would enumerate, in
    order: pass-1 zip batches, then pass-2 name pairs."""
    out: list[dict] = []
    zips = [f"{i:05d}" for i in zip_range]
    for i in range(0, len(zips), batch_size):
        out.append({"zips": zips[i : i + batch_size], "clientIp": ""})
    if name_sweep:
        for fi in NAME_LETTERS:
            for li in NAME_LETTERS:
                out.append(
                    {"firstName": fi, "lastName": li, "jurisdictions": [code], "clientIp": ""}
                )
    return out


def _expand_zips(query: dict) -> list[dict]:
    """Split a multi-zip batch into single-zip queries (the atomic unit resume
    mode can safely replay). Non-zip / single-zip queries pass through."""
    zips = query.get("zips")
    if not zips or len(zips) <= 1:
        return [query]
    return [{"zips": [z], "clientIp": ""} for z in zips]


def _load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _compute_pending(adapter) -> tuple[list[dict], dict]:
    """Return (pending_queries, stats) for one adapter, reading its run logs."""
    subdir = adapter.run_log_subdir
    code = adapter.jurisdiction_code
    run_dirs = sorted(d for d in (LOG_ROOT / subdir).glob("*/") if d.is_dir())
    if not run_dirs:
        raise SystemExit(f"no prior runs under {LOG_ROOT / subdir} — run a full ingest first")

    # Union successes across every prior run (a query that ever worked is done);
    # take failures from the latest run only (older fails may since have passed).
    # Union successes AND failures across every prior run. A query that ever
    # succeeded is done (filtered below via done_keys), so unioning failures is
    # safe and avoids forgetting a retryable failure from an intermediate run
    # that a later run never re-attempted. settled_117 = stable invalid zips.
    succeeded: list[dict] = []
    settled_117: set[tuple] = set()
    failed: list[dict] = []
    for d in run_dirs:
        succeeded.extend(_load_jsonl(d / "successful.jsonl"))
        for row in _load_jsonl(d / "failed.jsonl"):
            if row.get("retryable"):
                failed.append(row)
            else:
                settled_117.add(_canonical(row["query"]))
    done_keys = {_canonical(r["query"]) for r in succeeded}

    planned = _enumerate_planned(
        adapter.zip_range, adapter.batch_size, code, adapter.name_sweep
    )
    # Never-attempted = planned, minus what succeeded, minus settled invalid-zip
    # 117s (re-running those just re-fails). Genuinely-unrun queries only.
    # Expand multi-zip batches into per-zip singletons: resume mode replays each
    # query with a single _post_search (no 117/511 sub-split), so a batch with
    # one invalid zip would 117 and drop its valid zips. Singletons can't.
    never: list[dict] = []
    seen: set[tuple] = set()
    for q in planned:
        k = _canonical(q)
        if k in done_keys or k in settled_117:
            continue
        for sub in _expand_zips(q):
            sk = _canonical(sub)
            if sk in done_keys or sk in settled_117 or sk in seen:
                continue
            never.append(sub)
            seen.add(sk)
    never_count = len(never)

    pending = list(never)
    retryable = 0
    for row in failed:  # already retryable-only, unioned across all runs
        q = row["query"]
        k = _canonical(q)
        if k in done_keys or k in seen:
            continue
        pending.append(q)
        seen.add(k)
        retryable += 1

    stats = {
        "prior_runs": len(run_dirs),
        "latest_run": run_dirs[-1].name,
        "succeeded_union": len(succeeded),
        "retryable_failed_union": len(failed),
        "planned": len(planned),
        "never_attempted_expanded": never_count,
        "retryable_failures": retryable,
        "settled_117_invalid_zip": len(settled_117),
        "total_pending": len(pending),
    }
    return pending, stats


def _chunked_list(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def _run_chunk(cls, queries: list[dict], args) -> int:
    """Run one chunk with a FRESH adapter (= fresh browser + fresh Cloudflare
    session). Returns records upserted."""
    adapter = cls(
        headless=True,
        request_delay_s=args.delay,
        failure_backoff_s=args.backoff,
        renav_after_consecutive_failures=args.renav,
        progress_every=25,
        queries=queries,
    )
    n = 0
    with FileStore(REGISTRY_ROOT) as store:
        for record, photo_refs in adapter.run():
            store.upsert(record, photos=photo_refs)
            n += 1
            if n % 100 == 0:
                print(f"    upserted {n} ...", flush=True)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("states", nargs="+", help="adapter name(s), e.g. texas vermont wisconsin")
    ap.add_argument("--dry-run", action="store_true", help="compute + print pending, don't run")
    ap.add_argument("--delay", type=float, default=10.0, help="per-request delay s (default 10)")
    ap.add_argument("--backoff", type=float, default=30.0, help="extra sleep after a failure (default 30)")
    ap.add_argument("--renav", type=int, default=3, help="re-nav CF after N consecutive fails (default 3)")
    # CF enforces a per-session request budget (~200 queries observed): after it,
    # the whole browser session gets blocked and renav can't recover it. So run
    # in chunks, each on a fresh browser (fresh session), with a cooldown between
    # to let CF's per-IP rate budget refill. 0 disables chunking.
    ap.add_argument("--chunk", type=int, default=150, help="queries per fresh browser session (default 150; 0=off)")
    ap.add_argument("--cooldown", type=float, default=180.0, help="seconds between chunks (default 180)")
    args = ap.parse_args()

    grand_total = 0
    for name in args.states:
        adapter0 = _load_adapter(name)
        pending, stats = _compute_pending(adapter0)
        print(f"\n===== {name} ({adapter0.jurisdiction_code}) =====")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        grand_total += stats["total_pending"]
        if args.dry_run or not pending:
            if not pending:
                print("  nothing pending.")
            continue

        cls = type(adapter0)
        chunks = _chunked_list(pending, args.chunk) if args.chunk > 0 else [pending]
        total_n = 0
        for ci, chunk in enumerate(chunks, 1):
            print(f"  {name} chunk {ci}/{len(chunks)} ({len(chunk)} queries, fresh CF session)", flush=True)
            total_n += _run_chunk(cls, chunk, args)
            if ci < len(chunks) and args.cooldown > 0:
                print(f"  cooldown {args.cooldown:.0f}s before next chunk ...", flush=True)
                time.sleep(args.cooldown)
        print(f"  {name} resume complete: {total_n} new/updated records.", flush=True)

    if args.dry_run:
        est_hours = grand_total * args.delay / 3600.0
        print(f"\nDRY RUN: {grand_total} total pending queries "
              f"(~{est_hours:.1f}h at {args.delay:.0f}s/query, excl. failure backoff)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
