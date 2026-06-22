"""Run ONE enrich-details shard, auto-restarting on crash.

The HBase Thrift gateway can drop a connection mid-run (TSocket read 0 bytes);
enrich-details has no built-in reconnect, so we wrap it. The pass is idempotent
(already-filled persons are skipped without a fetch), so a restart just re-scans
its hash-slice and resumes filling gaps. Loops until a clean exit (code 0).

Usage:  python scripts/enrich_shard.py <index> <N> [pause] [backoff]
"""
import subprocess
import sys
import time
from datetime import datetime, timezone

REPO = r"C:\development\registry-faces"
EXE = REPO + r"\.venv\Scripts\registry-faces.exe"


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    index, n = sys.argv[1], sys.argv[2]
    pause = sys.argv[3] if len(sys.argv) > 3 else "0.5"
    backoff = int(sys.argv[4]) if len(sys.argv) > 4 else 15
    attempt = 0
    while True:
        attempt += 1
        print(f"=== [shard {index}/{n}] attempt {attempt} start {stamp()} ===", flush=True)
        code = subprocess.run(
            [EXE, "enrich-details", "--to", "hbase", "--kind", "registry",
             "--shard", f"{index}/{n}", "--pause", pause],
            cwd=REPO,
        ).returncode
        if code == 0:
            print(f"=== [shard {index}/{n}] DONE clean (exit 0) after {attempt} attempt(s) {stamp()} ===", flush=True)
            return 0
        print(f"=== [shard {index}/{n}] exit {code} — restarting in {backoff}s {stamp()} ===", flush=True)
        time.sleep(backoff)


if __name__ == "__main__":
    raise SystemExit(main())
