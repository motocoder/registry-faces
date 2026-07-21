"""Download shard bundles from Cloudflare R2 to a local dir.

The inverse of ``upload_shards.py``: pulls ``shards/US-XX/{manifest.json,
shard-*.zip}`` out of the R2 bucket so they can be unpacked and re-published
(see ``load_shards_identity.py``) without re-scraping the live registries.

* Treat each state's manifest as the authoritative inventory.
* Verify every shard by byte size and SHA-256 before publishing it locally.
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
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import boto3
    from botocore.client import Config
except ImportError:
    sys.stderr.write("download_shards.py requires boto3. Install: pip install -e '.[shards]'\n")
    sys.exit(2)

from dotenv import load_dotenv

from registry_faces.shards import (
    parse_manifest,
    publish_directory,
    sha256_file,
    verified_shard_paths,
)


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


def _remote_manifest_states(client, bucket: str, prefix: str) -> set[str]:
    """Return states that expose an authoritative ``manifest.json`` object."""

    states: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key.startswith(prefix):
                continue
            rel = key[len(prefix):]
            parts = rel.split("/")
            if (
                len(parts) == 2
                and re.fullmatch(r"US-[A-Z]{2}", parts[0]) is not None
                and parts[1] == "manifest.json"
            ):
                states.add(parts[0])
    return states


def _remote_manifest(client, bucket: str, prefix: str, state: str):
    """Fetch and validate a state's remote commit marker."""

    key = f"{prefix}{state}/manifest.json"
    response = client.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read()
    manifest = parse_manifest(payload, expected_state=state)
    return key, payload, manifest


def _local_bundle_current(state_dir: Path, manifest_bytes: bytes) -> bool:
    """True only when local manifest and exact local shard set are verified."""

    manifest_path = state_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.read_bytes() != manifest_bytes:
        return False
    try:
        paths = verified_shard_paths(state_dir)
    except ValueError:
        return False
    expected = {path.name for path in paths}
    actual = {path.name for path in state_dir.glob("shard-*.zip") if path.is_file()}
    return actual == expected


def _download_state(
    client,
    *,
    bucket: str,
    prefix: str,
    state: str,
    shards_dir: Path,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Stage and verify one complete state bundle, then publish it as a unit."""

    _manifest_key, manifest_bytes, manifest = _remote_manifest(
        client, bucket, prefix, state
    )
    state_dir = shards_dir / state
    if _local_bundle_current(state_dir, manifest_bytes):
        return 0, len(manifest.shards), 0

    def local_shard_ok(entry) -> bool:
        current = state_dir / entry.name
        return bool(
            not current.is_symlink()
            and current.is_file()
            and current.stat().st_size == entry.size_bytes
            and sha256_file(current) == entry.sha256
        )

    # A dry-run may inspect remote/local state, but must not create its output
    # root or a temporary staging directory.
    if dry_run:
        skipped = 0
        for entry in manifest.shards:
            if local_shard_ok(entry):
                skipped += 1
            else:
                print(
                    f"{state}/{entry.name} would download "
                    f"({entry.size_bytes / 1024 / 1024:.1f} MB)"
                )
        return 0, skipped, 0

    shards_dir.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{state}.download-", dir=shards_dir))
    published = False
    downloaded = skipped = total_bytes = 0
    try:
        for entry in manifest.shards:
            remote_key = f"{prefix}{state}/{entry.name}"
            current = state_dir / entry.name
            destination = staged / entry.name
            current_ok = local_shard_ok(entry)
            if current_ok:
                skipped += 1
                shutil.copy2(current, destination)
                continue

            client.download_file(bucket, remote_key, str(destination))
            actual_size = destination.stat().st_size
            if actual_size != entry.size_bytes:
                raise ValueError(
                    f"downloaded {remote_key} is {actual_size} bytes; "
                    f"manifest expects {entry.size_bytes}"
                )
            actual_digest = sha256_file(destination)
            if actual_digest != entry.sha256:
                raise ValueError(
                    f"downloaded {remote_key} sha256 is {actual_digest}; "
                    f"manifest expects {entry.sha256}"
                )
            downloaded += 1
            total_bytes += actual_size
            print(
                f"{state}/{entry.name} ({actual_size / 1024 / 1024:.1f} MB)",
                flush=True,
            )

        (staged / "manifest.json").write_bytes(manifest_bytes)
        # Re-read the staged contract before replacing the last known-good
        # local bundle.  This also ensures copied shards were not altered.
        verified_shard_paths(staged, expected_state=state)
        publish_directory(staged, state_dir)
        published = True
        return downloaded, skipped, total_bytes
    finally:
        if not published and staged.exists():
            shutil.rmtree(staged)


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

    remote_states = _remote_manifest_states(client, bucket, prefix)
    states = sorted(remote_states if want is None else remote_states & want)
    missing = sorted((want or set()) - remote_states)

    if args.list:
        print(f"bucket={bucket} prefix={prefix!r}  states={len(states)}")
        for state in states:
            print(f"  {state}")
        if missing:
            print(f"  missing requested manifests: {', '.join(missing)}")
        return 1 if missing else 0

    if not states:
        sys.stderr.write(f"no matching US-* shards in {bucket}/{prefix} "
                         f"{'for '+str(sorted(want)) if want else ''}\n")
        return 1

    downloaded = skipped = 0
    total_bytes = 0
    failures = len(missing)
    for state in states:
        try:
            got, kept, byte_count = _download_state(
                client,
                bucket=bucket,
                prefix=prefix,
                state=state,
                shards_dir=args.shards_dir,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - isolate state publications
            failures += 1
            sys.stderr.write(f"{state}: download failed: {exc}\n")
            continue
        downloaded += got
        skipped += kept
        total_bytes += byte_count

    print(f"\ndone: {downloaded} downloaded ({total_bytes/1024/1024:.1f} MB), "
          f"{skipped} already present -> {args.shards_dir.resolve()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
