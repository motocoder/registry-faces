"""Build shard bundles for the registry-recognizer Android client.

Reads per-state records from ``records/US-XX/<userdir>/{record.json, photos/*}``
and emits checksum-addressed ``shards/US-XX/{manifest.json, shard-*.zip}`` ready to copy to
``berserkr.llc``. Inside each shard zip the layout matches the client's
parser (RegistryDownloadManager.userKey):

    US-XX/<userdir>/record.json
    US-XX/<userdir>/photos/<filename>

Photos pass through Pillow on the way out:

* EXIF orientation baked in (no more sideways portraits arriving on-device)
* Long edge capped at ``--max-edge`` px (default 2560, matches the client's
  ``PhotoEnricher.MAX_DECODE_EDGE`` — anything bigger is dead weight)
* Re-encoded as JPEG q=85, ``optimize=True``

That normalization is where SD's ~1 MB/record drops toward WA's ~50 KB.

Records inside each state are sorted by ``identity.guid`` so a partial-
failed shard import on the client resumes against the same shard
boundaries the manifest declares.

Usage:
    pip install pillow
    .venv/bin/python scripts/package_shards.py
    .venv/bin/python scripts/package_shards.py --state US-SD --shard-size-mb 50
    .venv/bin/python scripts/package_shards.py --records-dir registry/records \\
                                               --shards-dir shards
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.stderr.write(
        "package_shards.py requires Pillow. Install with: pip install pillow\n"
    )
    sys.exit(2)


logger = logging.getLogger("package_shards")


# Files in a user's photos/ directory that should NOT make it into shards.
# `manifest.json` is registry-faces ingest metadata (the per-user photo
# log), not an image. The other entries are filesystem noise that
# occasionally slips into the tree.
PHOTO_SKIP_NAMES = {"manifest.json", ".DS_Store", "Thumbs.db"}


# Jurisdictions we never build shards for, regardless of what's on disk or
# what --state requests. These states prohibit redistribution of registry
# data, so their records must never leave the local store as shards. Sourced
# from the package-wide single source of truth so ingest, build, and upload
# can't drift apart.
from registry_faces.blacklist import BLACKLIST as SHARD_BLACKLIST
from registry_faces.shards import (
    content_addressed_shard_name,
    parse_manifest,
    publish_directory,
    sha256_file,
)


# ---------- types --------------------------------------------------------


@dataclass
class BundleMeta:
    """Pass-1 record metadata retained while the state is GUID-sorted.

    Only paths and short sort keys remain resident.  Pass 1 briefly parses each
    record for its GUID; pass 2 rereads that record beside its photos.  This
    keeps memory bounded even when record JSON contains large raw payloads.
    """

    # On-disk dir of this user — lets pass 2 reach the photos/ subdir
    # without re-walking from records-root.
    user_dir: Path
    # Folder name only; becomes the client-side `sourceId`. Long
    # adapter-derived slugs (URL-style) are fine — they're just zip
    # entry path segments, no length constraint downstream.
    userdir_name: str
    # Sort key. Falls back to `userdir_name` when record.json predates
    # the v5 schema (no `identity.guid`). Falling back keeps the sort
    # deterministic even on partial data.
    guid: str
    # Reread in pass 2 and shipped verbatim — no re-serialize.
    record_path: Path


@dataclass
class Photo:
    filename: str  # leaf, preserved from source (e.g. "001-registry.jpg")
    bytes: bytes   # post-normalize JPEG payload


@dataclass
class ShardInfo:
    name: str
    size_bytes: int
    sha256: str
    record_count: int


@dataclass
class Stats:
    users_seen: int = 0
    users_skipped_no_record: int = 0
    users_skipped_bad_json: int = 0
    users_skipped_unsafe_path: int = 0
    users_skipped_wrong_state: int = 0
    users_missing_guid: int = 0
    photos_normalized: int = 0
    photos_skipped: int = 0


# ---------- image normalization ----------------------------------------


def _safe_zip_segment(value: str) -> bool:
    """Whether a local leaf is safe as one portable ZIP path segment."""

    return bool(
        value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and all(ord(char) >= 32 for char in value)
    )


def normalize_image(src_bytes: bytes, max_edge: int, quality: int) -> bytes:
    """Decode → EXIF-transpose → optionally downsize → re-encode as JPEG.

    Always passes through Pillow so output bytes are consistent
    regardless of source pipeline. Even when the source is already
    within budget on size, normalization buys us EXIF rotation baked
    into pixels (BitmapFactory on Android ignores the orientation tag)
    and strips any oversized metadata blobs.
    """
    with Image.open(io.BytesIO(src_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        if max(img.size) > max_edge:
            # thumbnail() mutates in place, preserves aspect ratio,
            # and is no-op when both dimensions already fit.
            img.thumbnail((max_edge, max_edge), Image.LANCZOS)
        if img.mode not in ("RGB", "L"):
            # JPEG can't carry alpha. Registry photos are portrait
            # head-shots, so flattening transparency to white is fine —
            # the model only cares about face pixels.
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


# ---------- user-dir loading -------------------------------------------


def scan_meta(user_dir: Path, stats: Stats) -> Optional[BundleMeta]:
    """Pass 1: read just record.json and produce a sortable BundleMeta.
    No image I/O — that happens in pass 2 just-in-time so peak memory
    stays bounded to one user's photos at a time. FL's 91k records
    would otherwise want ~4 GB simultaneously held in RAM."""
    if user_dir.is_symlink() or not _safe_zip_segment(user_dir.name):
        logger.warning("skip %s — unsafe/symlinked user directory", user_dir)
        stats.users_skipped_unsafe_path += 1
        return None
    record_path = user_dir / "record.json"
    if record_path.is_symlink():
        logger.warning("skip %s — record.json may not be a symlink", user_dir.name)
        stats.users_skipped_unsafe_path += 1
        return None
    if not record_path.is_file():
        logger.warning("skip %s — no record.json", user_dir.name)
        stats.users_skipped_no_record += 1
        return None

    record_bytes = record_path.read_bytes()
    try:
        record_data = json.loads(record_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("skip %s — invalid record.json: %s", user_dir.name, e)
        stats.users_skipped_bad_json += 1
        return None

    if not isinstance(record_data, dict):
        logger.warning("skip %s — record.json must be an object", user_dir.name)
        stats.users_skipped_bad_json += 1
        return None

    source = record_data.get("source")
    actual_state = source.get("jurisdiction") if isinstance(source, dict) else None
    expected_state = user_dir.parent.name
    if actual_state != expected_state:
        logger.warning(
            "skip %s — record jurisdiction %r does not match %s",
            user_dir.name,
            actual_state,
            expected_state,
        )
        stats.users_skipped_wrong_state += 1
        return None

    guid = extract_guid(record_data)
    if not guid:
        # Legacy record without identity.guid. Falling back to the
        # adapter-derived userdir keeps the sort deterministic and
        # stable across re-runs (re-ingest doesn't rename userdirs).
        stats.users_missing_guid += 1
        guid = user_dir.name

    return BundleMeta(
        user_dir=user_dir,
        userdir_name=user_dir.name,
        guid=guid,
        record_path=record_path,
    )


def load_photos(
    meta: BundleMeta, max_edge: int, quality: int, stats: Stats
) -> list[Photo]:
    """Pass 2: load + normalize one user's photos on demand. Caller
    writes them to the current shard and discards — they don't
    accumulate across the iteration."""
    photos: list[Photo] = []
    photos_dir = meta.user_dir / "photos"
    if photos_dir.is_symlink():
        logger.warning("skip photos for %s — directory is a symlink", meta.userdir_name)
        stats.photos_skipped += 1
        return photos
    if not photos_dir.is_dir():
        return photos
    for img_path in sorted(photos_dir.iterdir()):
        if img_path.name in PHOTO_SKIP_NAMES:
            continue
        if (
            img_path.is_symlink()
            or not img_path.is_file()
            or not _safe_zip_segment(img_path.name)
        ):
            logger.warning(
                "skip image %s/%s — unsafe path or symlink",
                meta.userdir_name,
                img_path.name,
            )
            stats.photos_skipped += 1
            continue
        try:
            normalized = normalize_image(
                img_path.read_bytes(), max_edge, quality
            )
            photos.append(Photo(filename=img_path.name, bytes=normalized))
            stats.photos_normalized += 1
        except Exception as e:
            # One corrupt image shouldn't kill the user's record;
            # the client falls back to other photos via its
            # photoKeys map. Log + move on.
            logger.warning(
                "skip image %s/%s — %s", meta.userdir_name, img_path.name, e
            )
            stats.photos_skipped += 1
    return photos


def extract_guid(record_data: dict) -> str:
    """Pull `identity.guid` out of a parsed record.json. Returns empty
    string when the field is missing or non-string — caller substitutes
    the userdir name as the deterministic fallback."""
    identity = record_data.get("identity")
    if isinstance(identity, dict):
        guid = identity.get("guid")
        if isinstance(guid, str) and guid:
            return guid
    return ""


# ---------- shard packing ----------------------------------------------


def write_shards(
    state_code: str,
    metas: list[BundleMeta],
    out_dir: Path,
    shard_size_bytes: int,
    max_edge: int,
    quality: int,
    stats: Stats,
) -> list[ShardInfo]:
    """Pass 2: walk pre-sorted metas, load each user's photos JIT,
    write directly into the current shard, then drop the photo bytes.
    Opens a new shard whenever adding the next bundle would push the
    current one past the byte budget AND the current shard has at
    least one record (so a single over-budget user still lands in its
    own shard rather than failing).

    Records within a shard are written in input order — which is the
    GUID sort the caller passed in — so the manifest's shard
    boundaries are fully determined by (guid order, byte budget).
    """
    shards: list[ShardInfo] = []
    shard_index = 0

    # Mutable per-shard state held in closures over open_next /
    # close_current. Could be a class — kept as locals for proximity
    # to the only loop that touches them.
    current_zip: Optional[zipfile.ZipFile] = None
    current_path: Optional[Path] = None
    current_index: Optional[int] = None
    current_bytes_estimate = 0
    current_record_count = 0

    def write_entry(archive_name: str, payload: bytes) -> None:
        # ZipFile.writestr(str, ...) stamps wall-clock time. That makes identical
        # inputs hash differently on every build, defeating upload skips and
        # leaking an unbounded series of immutable objects into R2.
        info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        current_zip.writestr(info, payload)

    def open_next() -> None:
        nonlocal current_zip, current_path, current_index, shard_index
        nonlocal current_bytes_estimate, current_record_count
        # Unprefixed name; the state is conveyed by the containing
        # directory (shards/<state>/) which becomes the server URL
        # path segment (<base>/<state>/<name>).
        current_index = shard_index
        name = f"shard-{current_index:03d}.zip"
        shard_index += 1
        current_path = out_dir / name
        # ZIP_STORED — JPEG bytes don't compress meaningfully and
        # DEFLATE burns CPU on both ends for sub-percent size wins.
        current_zip = zipfile.ZipFile(current_path, "w", zipfile.ZIP_STORED)
        current_bytes_estimate = 0
        current_record_count = 0

    def close_current() -> None:
        nonlocal current_zip, current_path, current_index
        if current_zip is None:
            return
        current_zip.close()
        size = current_path.stat().st_size
        sha = hash_file(current_path)
        assert current_index is not None
        immutable_path = current_path.with_name(
            content_addressed_shard_name(current_index, sha)
        )
        current_path.rename(immutable_path)
        shards.append(
            ShardInfo(
                name=immutable_path.name,
                size_bytes=size,
                sha256=sha,
                record_count=current_record_count,
            )
        )
        logger.info(
            "closed %s: %d records, %.1f MB",
            immutable_path.name,
            current_record_count,
            size / 1024 / 1024,
        )
        current_zip = None
        current_path = None
        current_index = None

    try:
        for i, meta in enumerate(metas):
            record_json_bytes = meta.record_path.read_bytes()
            photos = load_photos(meta, max_edge, quality, stats)
            approx = len(record_json_bytes) + sum(len(p.bytes) for p in photos)
            if current_zip is None:
                open_next()
            elif (
                current_bytes_estimate + approx > shard_size_bytes
                and current_record_count > 0
            ):
                close_current()
                open_next()

            base = f"{state_code}/{meta.userdir_name}"
            write_entry(f"{base}/record.json", record_json_bytes)
            for photo in photos:
                write_entry(f"{base}/photos/{photo.filename}", photo.bytes)
            current_bytes_estimate += approx
            current_record_count += 1
            # photos goes out of scope at next iteration — its byte
            # payloads are released back to the allocator before the next
            # user's photos are decoded.
            if (i + 1) % 500 == 0:
                logger.info(
                    "[%s] wrote %d / %d records", state_code, i + 1, len(metas)
                )
    except BaseException:
        # Do not record a partially-written archive as a ShardInfo. Closing the
        # handle is essential on Windows before the staging tree can be removed.
        if current_zip is not None:
            current_zip.close()
        raise
    else:
        close_current()
    return shards


def hash_file(path: Path) -> str:
    """Backwards-compatible wrapper for the shared streaming digest helper."""

    return sha256_file(path)


# ---------- shard verification -----------------------------------------


def verify_shards(out_dir: Path, shards: list[ShardInfo]) -> bool:
    """Re-open each shard and confirm it's well-formed: opens as a zip,
    contains the expected number of `record.json` entries, no entry is
    zero-length. Returns True on full pass.

    The manifest's sha256 is the canonical client-side check; this is
    belt-and-suspenders so a corrupt shard is caught BEFORE it ships
    rather than after the client hits a CRC failure mid-import.
    """
    ok = True
    for s in shards:
        path = out_dir / s.name
        try:
            with zipfile.ZipFile(path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    logger.error("verify %s — corrupt entry %s", s.name, bad)
                    ok = False
                    continue
                record_count = sum(
                    1
                    for n in zf.namelist()
                    if n.endswith("/record.json")
                )
                if record_count != s.record_count:
                    logger.error(
                        "verify %s — manifest says %d records, zip has %d",
                        s.name,
                        s.record_count,
                        record_count,
                    )
                    ok = False
                empty = [zi.filename for zi in zf.infolist() if zi.file_size == 0]
                if empty:
                    logger.error(
                        "verify %s — %d empty entries (first 3: %s)",
                        s.name,
                        len(empty),
                        empty[:3],
                    )
                    ok = False
        except zipfile.BadZipFile as e:
            logger.error("verify %s — not a valid zip: %s", s.name, e)
            ok = False
    return ok


# ---------- per-state pipeline -----------------------------------------


def process_state(
    state_code: str,
    records_root: Path,
    out_root: Path,
    shard_size_bytes: int,
    max_edge: int,
    quality: int,
    version: str,
    verify: bool,
    allow_empty: bool = False,
) -> bool:
    if not re.fullmatch(r"US-[A-Z]{2}", state_code):
        logger.error("invalid state code %r; expected US-XX", state_code)
        return False
    state_records = records_root / state_code
    if state_records.is_symlink() or not state_records.is_dir():
        logger.error(
            "[%s] records dir missing or symlinked at %s — preserving prior bundle",
            state_code,
            state_records,
        )
        return False

    out_dir = out_root / state_code
    if state_records.resolve() == out_dir.resolve():
        logger.error("[%s] input and output state directories are identical", state_code)
        return False

    user_dirs = sorted(
        p for p in state_records.iterdir() if p.is_dir() and not p.is_symlink()
    )
    logger.info("[%s] scanning %d user dirs (pass 1: record.json only)",
                state_code, len(user_dirs))

    stats = Stats()
    # Pass 1: walk + parse record.json for every user. Cheap I/O,
    # constant per-user memory. Lets us sort the entire state by GUID
    # before any photo decoding starts.
    metas: list[BundleMeta] = []
    for i, user_dir in enumerate(user_dirs):
        stats.users_seen += 1
        m = scan_meta(user_dir, stats)
        if m is not None:
            metas.append(m)
        if (i + 1) % 5000 == 0:
            logger.info(
                "[%s] scanned %d / %d users", state_code, i + 1, len(user_dirs)
            )

    metas.sort(key=lambda m: (m.guid, m.userdir_name))
    logger.info(
        "[%s] sorted %d records by guid — starting pass 2 (photos + shards)",
        state_code, len(metas),
    )

    if not metas and not allow_empty:
        logger.error(
            "[%s] no valid records found; preserving the last published bundle",
            state_code,
        )
        return False

    out_root.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{state_code}.build-", dir=out_root))
    published = False
    try:
        # Pass 2: stream into a private sibling directory.  The previously
        # published bundle remains untouched until this one is complete.
        shards = write_shards(
            state_code, metas, staged, shard_size_bytes, max_edge, quality, stats
        )
        if not shards and not allow_empty:
            logger.error(
                "[%s] packaging produced no shards; preserving existing output",
                state_code,
            )
            return False

        manifest = {
            "stateCode": state_code,
            "version": version,
            "totalRecords": sum(s.record_count for s in shards),
            "shards": [
                {
                    "name": s.name,
                    "sizeBytes": s.size_bytes,
                    "sha256": s.sha256,
                    "recordCount": s.record_count,
                }
                for s in shards
            ],
        }

        # Integrity is mandatory before publication.  ``--no-verify`` remains
        # accepted for CLI compatibility but now only suppresses the progress
        # message; publishing an unchecked replacement is never safe.
        if verify:
            logger.info("[%s] verifying %d shards", state_code, len(shards))
        verified = verify_shards(staged, shards)
        if not verified:
            logger.error(
                "[%s] verification failed; preserving the last published bundle",
                state_code,
            )
            return False

        # The manifest is the commit marker and is created only after every
        # archive passes verification.  Validate our own serialized contract
        # before exposing it to download/load consumers.
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        parse_manifest(
            manifest_bytes,
            expected_state=state_code,
            require_nonempty=not allow_empty,
        )
        (staged / "manifest.json").write_bytes(manifest_bytes)

        publish_directory(staged, out_dir)
        published = True

        total_bytes = sum(s.size_bytes for s in shards)
        logger.info(
            "[%s] DONE — %d records, %d shards, %.1f MB total, "
            "skipped(no-record=%d bad-json=%d unsafe-path=%d wrong-state=%d "
            "missing-guid=%d) "
            "photos(normalized=%d skipped=%d) verified=%s",
            state_code,
            manifest["totalRecords"],
            len(shards),
            total_bytes / 1024 / 1024,
            stats.users_skipped_no_record,
            stats.users_skipped_bad_json,
            stats.users_skipped_unsafe_path,
            stats.users_skipped_wrong_state,
            stats.users_missing_guid,
            stats.photos_normalized,
            stats.photos_skipped,
            verified,
        )
        return True
    finally:
        if not published and staged.exists():
            shutil.rmtree(staged)


# ---------- main -------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--records-dir",
        type=Path,
        default=Path("registry/records"),
        help="Root of per-state input dirs (default: registry/records)",
    )
    parser.add_argument(
        "--shards-dir",
        type=Path,
        default=Path("shards"),
        help="Root of per-state output dirs (default: shards)",
    )
    parser.add_argument(
        "--state",
        action="append",
        help="State code(s) to process. Repeatable. "
        "Defaults to every US-* subdir under records-dir.",
    )
    parser.add_argument(
        "--shard-size-mb",
        type=int,
        default=50,
        help="Target uncompressed size per shard in MB (default 50). "
        "Shards may exceed this slightly because a record is never split.",
    )
    parser.add_argument(
        "--max-edge",
        type=int,
        default=2560,
        help="Cap on photo long-edge in pixels (default 2560). "
        "Matches the client's PhotoEnricher.MAX_DECODE_EDGE.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=85,
        help="JPEG quality for normalized photos (default 85).",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Deprecated compatibility flag; integrity verification remains mandatory.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Explicitly publish an empty state manifest. By default an empty "
        "input is treated as a failed build and the prior bundle is preserved.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Set log level to DEBUG."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.records_dir.is_dir():
        parser.error(f"records dir not found: {args.records_dir}")
    if args.records_dir.resolve() == args.shards_dir.resolve():
        parser.error("--records-dir and --shards-dir must be different directories")
    if args.shard_size_mb <= 0:
        parser.error("--shard-size-mb must be greater than zero")
    if args.max_edge <= 0:
        parser.error("--max-edge must be greater than zero")
    if not 1 <= args.jpeg_quality <= 95:
        parser.error("--jpeg-quality must be between 1 and 95")

    if args.state:
        # Dedupe + preserve user-specified order.
        states = list(dict.fromkeys(args.state))
    else:
        states = sorted(
            p.name
            for p in args.records_dir.iterdir()
            if p.is_dir() and p.name.startswith("US-")
        )
    if not states:
        parser.error(f"no US-* state dirs under {args.records_dir}")
    invalid_states = [state for state in states if not re.fullmatch(r"US-[A-Z]{2}", state)]
    if invalid_states:
        parser.error(
            "invalid --state value(s): " + ", ".join(repr(s) for s in invalid_states)
        )

    # Hard blacklist: drop these jurisdictions even when explicitly passed
    # via --state, so there's no way to package them by mistake.
    dropped = [s for s in states if s in SHARD_BLACKLIST]
    for s in dropped:
        logger.warning(
            "[%s] %s is blacklisted — refusing to build shards for it",
            s, SHARD_BLACKLIST[s],
        )
    states = [s for s in states if s not in SHARD_BLACKLIST]
    if not states:
        parser.error(
            "no buildable states left after applying the blacklist "
            f"({', '.join(sorted(SHARD_BLACKLIST))})"
        )

    args.shards_dir.mkdir(parents=True, exist_ok=True)
    shard_size_bytes = args.shard_size_mb * 1024 * 1024
    # Single version stamp across all states in this run so the
    # manifest's `version` field maps 1:1 with a packaging invocation.
    version = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    logger.info(
        "packaging %d state(s) %s with shard_size_mb=%d max_edge=%d JPEG_q=%d",
        len(states),
        states,
        args.shard_size_mb,
        args.max_edge,
        args.jpeg_quality,
    )

    overall_ok = True
    t0 = time.monotonic()
    for state_code in states:
        try:
            ok = process_state(
                state_code=state_code,
                records_root=args.records_dir,
                out_root=args.shards_dir,
                shard_size_bytes=shard_size_bytes,
                max_edge=args.max_edge,
                quality=args.jpeg_quality,
                version=version,
                verify=not args.no_verify,
                allow_empty=args.allow_empty,
            )
            overall_ok = overall_ok and ok
        except KeyboardInterrupt:
            logger.warning(
                "[%s] interrupted — partial output left in place", state_code
            )
            return 130
        except Exception as e:
            logger.exception("[%s] failed: %s", state_code, e)
            return 1

    logger.info(
        "ALL DONE in %.1f minutes (overall_ok=%s)",
        (time.monotonic() - t0) / 60,
        overall_ok,
    )
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
