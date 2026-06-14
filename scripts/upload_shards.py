"""Upload shard bundles to Cloudflare R2 with skip-if-unchanged semantics.

Python sibling of :mod:`scripts.package_shards`: where ``package_shards.py``
*produces* ``shards/US-XX/{manifest.json, shard-NNN.zip}``, this script
*ships* them to the R2 bucket the registry-recognizer Android client reads
from.

Mirrors the semantics of the registry-recognizer ``./gradlew uploadShards``
Java task (``app/src/main/java/llc/berserkr/registryrecognizer/tools/
ShardUploader.java``):

* Walks SHARDS_FOLDER recursively, treating each file's path relative to
  the folder root as the R2 object key under SHARDS_BUCKET.
* HEAD per file: if R2 already has the object **with the same byte size**,
  SKIP. Otherwise PUT. Size-match is the cheap "already uploaded" test;
  it won't catch content changes that preserve byte size (uncommon for
  zip archives, but possible for json manifests).
* Additive — never deletes remote-only files.
* Concurrent — ``--parallelism`` (default 8) per-file uploads via a thread
  pool.

Usage::

    pip install -e ".[shards]"    # pulls in boto3
    .venv/bin/python scripts/upload_shards.py
    .venv/bin/python scripts/upload_shards.py --dry-run
    .venv/bin/python scripts/upload_shards.py --parallelism 16
    .venv/bin/python scripts/upload_shards.py --shards-dir ./shards \\
                                              --bucket registry-recognizer/shards/

Credentials and bucket come from .env (loaded via python-dotenv). See
.env.example for the required keys.

Exit code: number of files that failed to upload. Zero on full success.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _head_size(client, bucket: str, key: str) -> int | None:
    """Return remote object size, or None if absent / inaccessible."""
    try:
        resp = client.head_object(Bucket=bucket, Key=key)
        return int(resp["ContentLength"])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # 404 → object absent (expected for new uploads). Anything else is
        # a real problem the caller should know about.
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _upload_one(
    client,
    bucket: str,
    prefix: str,
    local_file: Path,
    rel: str,
    idx: int,
    total: int,
    dry_run: bool,
) -> tuple[bool, str]:
    """Upload one file. Returns (success, status_line). Status line mirrors
    the Java ShardUploader output for grep-friendly logs."""
    key = prefix + rel
    label = f"[{idx}/{total}] {rel:40s}"
    size = local_file.stat().st_size
    try:
        remote_size = _head_size(client, bucket, key)
        if remote_size == size:
            return True, f"{label} unchanged                SKIP"
        if dry_run:
            return True, f"{label} would upload ({size} bytes)  DRY-RUN"
        client.upload_file(
            str(local_file),
            bucket,
            key,
            ExtraArgs={"ContentType": _content_type(local_file.name)},
        )
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
    files = _walk_files(shards_dir)

    print("upload_shards.py:")
    print(f"  SHARDS_FOLDER = {shards_dir.resolve()}")
    print(f"  bucket        = {bucket}")
    print(f"  key prefix    = {prefix!r}")
    print(f"  endpoint      = {endpoint}")
    print(f"  parallelism   = {args.parallelism}")
    if args.dry_run:
        print("  DRY RUN (no PUTs will be sent)")
    print(f"walking shards folder → {len(files)} files")

    uploaded = skipped = failed = 0
    total = len(files)

    with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        futures = {
            pool.submit(
                _upload_one,
                client,
                bucket,
                prefix,
                f,
                f.relative_to(shards_dir).as_posix(),
                i + 1,
                total,
                args.dry_run,
            ): f
            for i, f in enumerate(files)
        }
        for fut in as_completed(futures):
            ok, line = fut.result()
            print(line)
            if not ok:
                failed += 1
            elif "SKIP" in line:
                skipped += 1
            else:
                uploaded += 1

    print()
    print(
        f"summary: {total} total, {uploaded} uploaded, {skipped} skipped, "
        f"{failed} failed"
    )
    return failed


if __name__ == "__main__":
    sys.exit(main())
