"""Resume / retry runner for the Washington adapter.

Reads the logs from a prior `registry-faces ingest washington` run,
computes the queries that are still pending (never-attempted +
retryable failures), and runs just those, merging new records into the
same FileStore.

Usage:
    .venv/bin/python scripts/wa_resume.py [PRIOR_LOG_DIR]

If no log dir is given, uses the most recent registry-runs/washington/*.
Output is written to a fresh registry-runs/washington/<timestamp>/ so the
audit trail is per-run.

Pending queries are computed as:
  (planned_enumeration) - (already_succeeded) + (retryable_failures)

`planned_enumeration` = all zip-batches + all name-pairs, in the same
order the adapter would generate them. `retryable_failures` come from
the failed.jsonl with retryable=True (we skip api_117 since those zips
simply don't exist).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from registry_faces.adapters.washington import (
    NAME_LETTERS,
    WashingtonAdapter,
    ZIP_RANGE,
    BATCH_SIZE,
)
from registry_faces.store import FileStore

REGISTRY_ROOT = Path("registry")
LOG_ROOT = Path("registry-runs/washington")


def _canonical(query: dict) -> tuple:
    """Stable key for dedup. Ignores clientIp (always empty/server-set)."""
    if "zips" in query:
        return ("zip", tuple(sorted(query["zips"])))
    if "firstName" in query:
        return ("name", query.get("firstName", ""), query.get("lastName", ""))
    return ("unknown", json.dumps(query, sort_keys=True))


def _enumerate_planned() -> list[dict]:
    """Reproduce the full set of queries the adapter would enumerate.
    Order: pass 1 zip batches, then pass 2 name pairs."""
    out: list[dict] = []
    zips = [f"{i:05d}" for i in ZIP_RANGE]
    for i in range(0, len(zips), BATCH_SIZE):
        out.append({"zips": zips[i : i + BATCH_SIZE], "clientIp": ""})
    for fi in NAME_LETTERS:
        for li in NAME_LETTERS:
            out.append(
                {
                    "firstName": fi,
                    "lastName": li,
                    "jurisdictions": ["WA"],
                    "clientIp": "",
                }
            )
    return out


def _load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def main() -> int:
    # Union the success log from *every* prior run so we don't re-run
    # queries that already worked in an earlier pass. For failed queries
    # we only take the most recent run's failures (older failures may
    # have been resolved by a later pass).
    if len(sys.argv) > 1:
        prior_dirs = [Path(sys.argv[1])]
    else:
        prior_dirs = sorted([p for p in LOG_ROOT.glob("*/") if p.is_dir()])
        if not prior_dirs:
            print(f"no prior runs found under {LOG_ROOT}", file=sys.stderr)
            return 1
    print(f"prior runs: {len(prior_dirs)}")
    for d in prior_dirs:
        print(f"  {d}")

    succeeded: list[dict] = []
    for d in prior_dirs:
        succeeded.extend(_load_jsonl(d / "successful.jsonl"))
    failed = _load_jsonl(prior_dirs[-1] / "failed.jsonl")
    print(f"  succeeded (union): {len(succeeded)}  failed (latest): {len(failed)}")

    done_keys = {_canonical(r["query"]) for r in succeeded}
    planned = _enumerate_planned()
    print(f"  planned total: {len(planned)}")

    # 1. Never-attempted: planned minus done.
    pending: list[dict] = [q for q in planned if _canonical(q) not in done_keys]

    # 2. Retryable failures we'd want to re-run. Dedup so we don't repeat.
    seen_pending = {_canonical(q) for q in pending}
    retry_count = 0
    for row in failed:
        if not row.get("retryable"):
            continue
        q = row["query"]
        k = _canonical(q)
        if k in done_keys or k in seen_pending:
            continue
        pending.append(q)
        seen_pending.add(k)
        retry_count += 1

    print(f"  never-attempted: {len(pending) - retry_count}")
    print(f"  retryable failures to re-run: {retry_count}")
    print(f"  total pending: {len(pending)}")

    if not pending:
        print("nothing to do.")
        return 0

    # Run with slower delays since we already heated up CF.
    adapter = WashingtonAdapter(
        headless=True,
        request_delay_s=10.0,
        failure_backoff_s=30.0,
        renav_after_consecutive_failures=3,
        progress_every=25,
        queries=pending,
    )

    with FileStore(REGISTRY_ROOT) as store:
        n = 0
        for record, photo_refs in adapter.run():
            store.upsert(record, photos=photo_refs)
            n += 1
            if n % 100 == 0:
                print(f"  upserted {n} ...", flush=True)
    print(f"resume complete. {n} new/updated records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
