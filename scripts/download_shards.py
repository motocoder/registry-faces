"""Download shard bundles from Cloudflare R2 to a local dir.

The inverse of ``upload_shards.py``: pulls ``shards/US-XX/{manifest.json,
shard-NNN.zip}`` out of the R2 bucket so they can be unpacked and re-published
(see ``load_shards_identity.py``) without re-scraping the live registries.

* Skip-if-unchanged by byte size (same cheap test the uploader uses).
* ``--state`` (repeatable) restricts to specific jurisdictions; default = all.
* Skips non ``US-*`` keys (e.g. the ``TEST``/``TEST2`` scratch shards).

Credentials + bucket come from .env (R2_BASE_URL / R2_ACCESS_KEY_ID /
R2_SECRET_ACCESS_KEY / SHARDS_BUCKET). See .env.example.

Usage:
  .venv/Scripts/python.exe scripts/download_shards.py --list
  .venv/Scripts/python.exe scripts/download_shards.py --state US-MN
  .venv/Scripts/python.exe scripts/download_shards.py            # everything
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import boto3
    from botocore.client import Config
except ImportError:
    sys.stderr.write("download_shards.py requires boto3. Install: pip install -e '.[shards]'\n")
    sys.exit(2)

from dotenv import load_dotenv


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        sys.stderr.write(f"missing required environment variable {key} (set in .env)\n")
        sys.exit(2)
    return val


def _split_bucket(raw: str) -> tuple[str, str]:
    raw = raw.strip().strip("/")
    if "/" in raw:
        bucket, prefix = raw.split("/", 1)
        return bucket, prefix.rstrip("/") + "/"
    return raw, ""


def _client(endpoint: str, access: str, secret: str):
    return boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access,
        aws_secret_access_key=secret, region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download shards from R2.")
    parser.add_argument("--shards-dir", type=Path, default=Path("shards"),
                        help="Local destination root (default ./shards).")
    parser.add_argument("--state", action="append", default=None,
                        help="Jurisdiction code(s) to fetch, e.g. US-MN. Repeatable. Default: all US-*.")
    parser.add_argument("--bucket", default=None, help="Override SHARDS_BUCKET.")
    parser.add_argument("--list", action="store_true", help="List remote inventory and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would download, no writes.")
    args = parser.parse_args(argv)

    load_dotenv()
    client = _client(_require_env("R2_BASE_URL"), _require_env("R2_ACCESS_KEY_ID"),
                     _require_env("R2_SECRET_ACCESS_KEY"))
    bucket, prefix = _split_bucket(args.bucket or _require_env("SHARDS_BUCKET"))
    want = {s.upper() for s in args.state} if args.state else None

    # Gather remote objects, grouped by state (top path segment under prefix).
    objects: list[tuple[str, str, int]] = []  # (key, state, size)
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            rel = o["Key"][len(prefix):]
            parts = rel.split("/")
            if len(parts) < 2 or not parts[0]:
                continue
            state = parts[0]
            if not state.startswith("US-"):  # skip TEST/TEST2 scratch shards
                continue
            if want and state not in want:
                continue
            objects.append((o["Key"], state, o["Size"]))

    if args.list:
        by_state: dict[str, list[int]] = {}
        for _key, state, size in objects:
            by_state.setdefault(state, []).append(size)
        print(f"bucket={bucket} prefix={prefix!r}  states={len(by_state)}")
        for state in sorted(by_state):
            sizes = by_state[state]
            print(f"  {state}: {len(sizes)} objects, {sum(sizes)/1024/1024:.1f} MB")
        return 0

    if not objects:
        sys.stderr.write(f"no matching US-* shards in {bucket}/{prefix} "
                         f"{'for '+str(sorted(want)) if want else ''}\n")
        return 1

    downloaded = skipped = 0
    total_bytes = 0
    for i, (key, _state, size) in enumerate(sorted(objects), 1):
        rel = key[len(prefix):]
        dest = args.shards_dir / rel
        label = f"[{i}/{len(objects)}] {rel}"
        if dest.exists() and dest.stat().st_size == size:
            skipped += 1
            continue
        if args.dry_run:
            print(f"{label}  would download ({size/1024/1024:.1f} MB)")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, key, str(dest))
        downloaded += 1
        total_bytes += size
        print(f"{label}  ({size/1024/1024:.1f} MB)", flush=True)

    print(f"\ndone: {downloaded} downloaded ({total_bytes/1024/1024:.1f} MB), "
          f"{skipped} already present -> {args.shards_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
