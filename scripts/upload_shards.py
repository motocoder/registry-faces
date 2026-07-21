"""Upload shard bundles to Cloudflare R2 with skip-if-unchanged semantics.

Python sibling of :mod:`scripts.package_shards`: where ``package_shards.py``
*produces* ``shards/US-XX/{manifest.json, shard-NNN-<sha256>.zip}``, this script
*ships* them to the R2 bucket the registry-recognizer Android client reads
from.

Mirrors the semantics of the registry-recognizer ``./gradlew uploadShards``
Java task (``app/src/main/java/llc/berserkr/registryrecognizer/tools/
ShardUploader.java``):

* Walks SHARDS_FOLDER recursively, treating each file's path relative to
  the folder root as the R2 object key under SHARDS_BUCKET.
* HEAD per shard: skip only when size and publisher SHA-256 metadata match.
  Equal-size content changes are therefore uploaded correctly.
* Additive — never deletes remote-only files.
* Shards upload concurrently, followed by each state manifest as the commit
  marker.  A manifest is never published when any shard upload fails.

Usage::

    pip install -e ".[shards]"    # pulls in boto3
    .venv/bin/python scripts/upload_shards.py
    .venv/bin/python scripts/upload_shards.py --dry-run
    .venv/bin/python scripts/upload_shards.py --parallelism 16
    .venv/bin/python scripts/upload_shards.py --shards-dir ./shards \\
                                              --bucket registry-recognizer/shards/

Credentials and bucket come from .env (loaded via python-dotenv). See
.env.example for the required keys.

Exit code: zero on full success, one when any upload fails.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
except ImportError:
    sys.stderr.write(
        "upload_shards.py requires boto3. Install with: pip install -e '.[shards]' "
        "(or `pip install boto3`)\n"
    )
    sys.exit(2)

from dotenv import load_dotenv

from registry_faces.blacklist import BLACKLIST
from registry_faces.shards import (
    is_content_addressed_shard_name,
    load_local_manifest,
    sha256_file,
    verified_shard_paths,
)


logger = logging.getLogger("upload_shards")


def _require_env(key: str) -> str:
    """Pull a required value out of the env, fail-fast with a clear message."""
    val = os.environ.get(key)
    if not val:
        sys.stderr.write(
            f"missing required environment variable {key} (set in .env or shell)\n"
        )
        sys.exit(2)
    return val


def _split_bucket(raw: str) -> tuple[str, str]:
    """Split a ``bucket/prefix/`` string into ``(bucket, prefix)``.

    SHARDS_BUCKET in local.properties is conventionally
    ``registry-recognizer/shards/`` — bucket name followed by a path
    prefix. boto3's S3 API wants them separate, so split at the first
    slash. A bare bucket name with no prefix is also accepted.
    """
    raw = raw.strip().strip("/")
    if "/" in raw:
        bucket, prefix = raw.split("/", 1)
        return bucket, prefix.rstrip("/") + "/"
    return raw, ""


def _build_client(endpoint: str, access: str, secret: str):
    # R2 ignores region but botocore requires *some* string; "auto" is the
    # convention Cloudflare publishes. Sig V4 is the only signer R2 accepts.
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


# OS-metadata litter macOS / Windows sprinkle into checked-out trees. Never
# belongs in a published shards bucket; silently drop on the way through.
_JUNK_NAMES = {".DS_Store", "Thumbs.db"}


def _walk_files(root: Path) -> list[Path]:
    """Every regular file under ``root``, sorted for deterministic output.
    Filters ``.DS_Store`` / ``Thumbs.db`` junk."""
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.name not in _JUNK_NAMES
    )


def _head_info(client, bucket: str, key: str) -> tuple[int | None, str | None]:
    """Return remote object size and publisher SHA-256, or absence markers."""

    try:
        resp = client.head_object(Bucket=bucket, Key=key)
        metadata = resp.get("Metadata") or {}
        digest = metadata.get("sha256")
        return int(resp["ContentLength"]), digest.lower() if digest else None
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # 404 → object absent (expected for new uploads). Anything else is
        # a real problem the caller should know about.
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None, None
        raise


@dataclass(frozen=True)
class UploadItem:
    path: Path
    rel: str
    sha256: str
    is_manifest: bool = False


def _publication_items(root: Path) -> tuple[list[UploadItem], list[UploadItem]]:
    """Collect only verified, manifest-listed state bundles.

    Stale local ``shard-*.zip`` files are intentionally ignored.  The manifest
    is the sole publication inventory and is returned in a second list so the
    caller can upload it only after all data objects succeed.
    """

    shards: list[UploadItem] = []
    manifests: list[UploadItem] = []
    for state_dir in sorted(
        path for path in root.iterdir() if path.is_dir() and path.name.startswith("US-")
    ):
        if state_dir.name in BLACKLIST:
            raise ValueError(
                f"{state_dir.name} is blacklisted ({BLACKLIST[state_dir.name]})"
            )
        manifest = load_local_manifest(state_dir)
        paths = verified_shard_paths(state_dir)
        for entry, path in zip(manifest.shards, paths, strict=True):
            if not is_content_addressed_shard_name(entry.name, entry.sha256):
                raise ValueError(
                    f"{state_dir.name}/{entry.name} uses a mutable legacy name; "
                    "re-run package_shards.py before uploading"
                )
            shards.append(
                UploadItem(
                    path=path,
                    rel=path.relative_to(root).as_posix(),
                    sha256=entry.sha256,
                )
            )
        manifest_path = state_dir / "manifest.json"
        manifests.append(
            UploadItem(
                path=manifest_path,
                rel=manifest_path.relative_to(root).as_posix(),
                sha256=sha256_file(manifest_path),
                is_manifest=True,
            )
        )
    if not manifests:
        raise ValueError(f"no valid US-* manifests under {root}")
    return shards, manifests


def _upload_one(
    client,
    bucket: str,
    prefix: str,
    local_file: Path,
    rel: str,
    idx: int,
    total: int,
    dry_run: bool,
    *,
    checksum: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Upload one file. Returns (success, status_line). Status line mirrors
    the Java ShardUploader output for grep-friendly logs."""
    key = prefix + rel
    label = f"[{idx}/{total}] {rel:40s}"
    size = local_file.stat().st_size
    checksum = checksum or sha256_file(local_file)
    try:
        actual_checksum = sha256_file(local_file)
        if actual_checksum != checksum:
            return False, (
                f"{label} local file changed after inventory "
                f"({actual_checksum} != {checksum})"
            )
        remote_size, remote_checksum = _head_info(client, bucket, key)
        if not force and remote_size == size and remote_checksum == checksum:
            return True, f"{label} unchanged                SKIP"
        if dry_run:
            return True, f"{label} would upload ({size} bytes)  DRY-RUN"
        client.upload_file(
            str(local_file),
            bucket,
            key,
            ExtraArgs={
                "ContentType": _content_type(local_file.name),
                "Metadata": {"sha256": checksum},
            },
        )
        final_size = local_file.stat().st_size
        final_checksum = sha256_file(local_file)
        if final_size != size or final_checksum != checksum:
            # The immutable key is still uncommitted because manifests are a
            # later phase.  Fail closed so this potentially inconsistent object
            # is never referenced; a retry can safely repair the same key.
            return False, f"{label} local file changed during upload"
        return True, f"{label} uploaded ({size} bytes) OK"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "?")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        return False, f"{label} upload failed code={code} ({msg})"
    except Exception as exc:  # pragma: no cover — network / fs surprises
        return False, f"{label} upload threw: {exc}"


def _content_type(name: str) -> str:
    n = name.lower()
    if n.endswith(".json"):
        return "application/json"
    if n.endswith(".zip"):
        return "application/zip"
    if n.endswith(".jpg") or n.endswith(".jpeg"):
        return "image/jpeg"
    return "application/octet-stream"


def _upload_phase(
    *,
    client,
    bucket: str,
    prefix: str,
    items: list[UploadItem],
    parallelism: int,
    dry_run: bool,
    start_index: int,
    total: int,
    force: bool = False,
) -> tuple[int, int, int]:
    """Upload one dependency-ordered phase and return uploaded/skipped/failed."""

    uploaded = skipped = failed = 0
    if not items:
        return uploaded, skipped, failed
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {
            pool.submit(
                _upload_one,
                client,
                bucket,
                prefix,
                item.path,
                item.rel,
                start_index + offset,
                total,
                dry_run,
                checksum=item.sha256,
                force=force,
            ): item
            for offset, item in enumerate(items)
        }
        for future in as_completed(futures):
            ok, line = future.result()
            print(line)
            if not ok:
                failed += 1
            elif "SKIP" in line:
                skipped += 1
            else:
                uploaded += 1
    return uploaded, skipped, failed


def _publish_inventory(
    *,
    client,
    bucket: str,
    prefix: str,
    shard_items: list[UploadItem],
    manifest_items: list[UploadItem],
    parallelism: int,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Publish data first and manifests only after a fully successful phase."""

    total = len(shard_items) + len(manifest_items)
    uploaded, skipped, failed = _upload_phase(
        client=client,
        bucket=bucket,
        prefix=prefix,
        items=shard_items,
        parallelism=parallelism,
        dry_run=dry_run,
        start_index=1,
        total=total,
    )
    if failed:
        for item in manifest_items:
            print(f"WITHHELD {item.rel}: one or more shard uploads failed")
        return uploaded, skipped, failed + len(manifest_items)

    manifest_uploaded, manifest_skipped, manifest_failed = _upload_phase(
        client=client,
        bucket=bucket,
        prefix=prefix,
        items=manifest_items,
        parallelism=parallelism,
        dry_run=dry_run,
        start_index=len(shard_items) + 1,
        total=total,
        force=True,
    )
    return (
        uploaded + manifest_uploaded,
        skipped + manifest_skipped,
        failed + manifest_failed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload shards to R2 (skip-if-unchanged, additive).",
    )
    parser.add_argument(
        "--shards-dir",
        type=Path,
        default=None,
        help="Local shards root (default: SHARDS_FOLDER env or ./shards).",
    )
    parser.add_argument(
        "--bucket",
        type=str,
        default=None,
        help="R2 bucket (or bucket/prefix/). Default: SHARDS_BUCKET env.",
    )
    parser.add_argument(
        "--parallelism", type=int, default=8, help="Concurrent uploads (default 8)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="HEAD-only pass; print what would change without PUT-ing.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug logging."
    )
    args = parser.parse_args(argv)

    if args.parallelism < 1:
        parser.error("--parallelism must be at least 1")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    load_dotenv()
    load_dotenv(".env.local", override=True)

    endpoint = _require_env("R2_BASE_URL")
    access = _require_env("R2_ACCESS_KEY_ID")
    secret = _require_env("R2_SECRET_ACCESS_KEY")

    bucket_raw = args.bucket or os.environ.get("SHARDS_BUCKET")
    if not bucket_raw:
        sys.stderr.write(
            "missing --bucket or SHARDS_BUCKET (e.g. registry-recognizer/shards/)\n"
        )
        return 2
    bucket, prefix = _split_bucket(bucket_raw)

    shards_dir = (
        args.shards_dir
        or (Path(os.environ["SHARDS_FOLDER"]) if os.environ.get("SHARDS_FOLDER") else None)
        or Path("shards")
    )
    if not shards_dir.is_dir():
        sys.stderr.write(f"shards dir not found: {shards_dir}\n")
        return 2

    client = _build_client(endpoint, access, secret)
    try:
        shard_items, manifest_items = _publication_items(shards_dir)
    except ValueError as exc:
        sys.stderr.write(f"refusing to upload invalid shard inventory: {exc}\n")
        return 2

    print("upload_shards.py:")
    print(f"  SHARDS_FOLDER = {shards_dir.resolve()}")
    print(f"  bucket        = {bucket}")
    print(f"  key prefix    = {prefix!r}")
    print(f"  endpoint      = {endpoint}")
    print(f"  parallelism   = {args.parallelism}")
    if args.dry_run:
        print("  DRY RUN (no PUTs will be sent)")
    total = len(shard_items) + len(manifest_items)
    print(
        f"verified shard inventory → {len(shard_items)} shards, "
        f"{len(manifest_items)} manifests"
    )

    uploaded, skipped, failed = _publish_inventory(
        client=client,
        bucket=bucket,
        prefix=prefix,
        shard_items=shard_items,
        manifest_items=manifest_items,
        parallelism=args.parallelism,
        dry_run=args.dry_run,
    )

    print()
    print(
        f"summary: {total} total, {uploaded} uploaded, {skipped} skipped, "
        f"{failed} failed"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
