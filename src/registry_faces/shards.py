"""Validation and publication helpers for registry shard bundles.

The manifest is the commit point for a state bundle.  Consumers must use the
exact shard list it declares and verify every size and digest before loading
data.  Keeping that contract here prevents the package, download, and identity
load scripts from drifting into subtly different interpretations.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SHARD_NAME_RE = re.compile(
    r"shard-(?P<index>[0-9]+)(?:-(?P<digest>[0-9a-f]{64}))?\.zip\Z"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ShardEntry:
    """One immutable object declared by a state manifest."""

    name: str
    size_bytes: int
    sha256: str
    record_count: int


@dataclass(frozen=True)
class ShardManifest:
    """Validated manifest fields used by shard producers and consumers."""

    state_code: str
    version: str
    total_records: int
    shards: tuple[ShardEntry, ...]


def sha256_file(path: Path) -> str:
    """Return a file's SHA-256 without retaining its contents in memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_addressed_shard_name(index: int, digest: str) -> str:
    """Return the immutable object name for one packaged shard."""

    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("shard index must be a non-negative integer")
    normalized = str(digest).lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("shard digest must be a SHA-256 hex string")
    return f"shard-{index:03d}-{normalized}.zip"


def is_content_addressed_shard_name(name: str, digest: str) -> bool:
    """Whether ``name`` embeds the complete declared SHA-256 digest."""

    match = _SHARD_NAME_RE.fullmatch(name)
    return bool(match and match.group("digest") == str(digest).lower())


def parse_manifest(
    payload: bytes | str | dict[str, Any],
    *,
    expected_state: str | None = None,
    require_nonempty: bool = True,
) -> ShardManifest:
    """Parse and strictly validate an on-disk or remote shard manifest.

    Shard names are restricted to legacy ``shard-NNN.zip`` or immutable
    ``shard-NNN-<sha256>.zip`` leaves so a crafted manifest cannot make a
    downloader or loader escape its state directory.
    """

    if isinstance(payload, bytes):
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid manifest JSON: {exc}") from exc
    elif isinstance(payload, str):
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid manifest JSON: {exc}") from exc
    else:
        raw = payload

    if not isinstance(raw, dict):
        raise ValueError("manifest must be a JSON object")

    state = raw.get("stateCode")
    if not isinstance(state, str) or not re.fullmatch(r"US-[A-Z]{2}", state):
        raise ValueError("manifest stateCode must be a US-XX jurisdiction")
    if expected_state is not None and state != expected_state:
        raise ValueError(
            f"manifest stateCode {state!r} does not match directory {expected_state!r}"
        )

    version = raw.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest version must be a non-empty string")

    declared_total = raw.get("totalRecords")
    if isinstance(declared_total, bool) or not isinstance(declared_total, int):
        raise ValueError("manifest totalRecords must be an integer")
    if declared_total < 0:
        raise ValueError("manifest totalRecords cannot be negative")

    raw_shards = raw.get("shards")
    if not isinstance(raw_shards, list):
        raise ValueError("manifest shards must be a list")
    if require_nonempty and not raw_shards:
        raise ValueError("manifest contains no shards")

    entries: list[ShardEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_shards):
        if not isinstance(item, dict):
            raise ValueError(f"manifest shard {index} must be an object")
        name = item.get("name")
        name_match = (
            _SHARD_NAME_RE.fullmatch(name) if isinstance(name, str) else None
        )
        if name_match is None:
            raise ValueError(f"manifest shard {index} has an unsafe name")
        if name in seen:
            raise ValueError(f"manifest declares duplicate shard {name!r}")
        seen.add(name)

        size = item.get("sizeBytes")
        count = item.get("recordCount")
        digest = item.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError(f"manifest shard {name!r} has invalid sizeBytes")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"manifest shard {name!r} has invalid recordCount")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest.lower()):
            raise ValueError(f"manifest shard {name!r} has invalid sha256")
        embedded_digest = name_match.group("digest")
        if embedded_digest is not None and embedded_digest != digest.lower():
            raise ValueError(
                f"manifest shard {name!r} does not match its declared sha256"
            )
        entries.append(
            ShardEntry(
                name=name,
                size_bytes=size,
                sha256=digest.lower(),
                record_count=count,
            )
        )

    actual_total = sum(entry.record_count for entry in entries)
    if actual_total != declared_total:
        raise ValueError(
            f"manifest totalRecords is {declared_total}, shard total is {actual_total}"
        )

    return ShardManifest(
        state_code=state,
        version=version,
        total_records=declared_total,
        shards=tuple(entries),
    )


def load_local_manifest(
    state_dir: Path,
    *,
    expected_state: str | None = None,
    require_nonempty: bool = True,
) -> ShardManifest:
    """Load a state's manifest and bind it to the containing directory name."""

    path = state_dir / "manifest.json"
    if state_dir.is_symlink():
        raise ValueError(f"shard state directory may not be a symlink: {state_dir}")
    if path.is_symlink():
        raise ValueError(f"shard manifest may not be a symlink: {path}")
    if not path.is_file():
        raise ValueError(f"missing shard manifest: {path}")
    return parse_manifest(
        path.read_bytes(),
        expected_state=expected_state or state_dir.name,
        require_nonempty=require_nonempty,
    )


def verified_shard_paths(
    state_dir: Path,
    *,
    expected_state: str | None = None,
    require_nonempty: bool = True,
) -> list[Path]:
    """Return only manifest-listed shards after checking size and SHA-256."""

    manifest = load_local_manifest(
        state_dir,
        expected_state=expected_state,
        require_nonempty=require_nonempty,
    )
    paths: list[Path] = []
    for entry in manifest.shards:
        path = state_dir / entry.name
        if path.is_symlink():
            raise ValueError(f"manifest-listed shard may not be a symlink: {path}")
        if not path.is_file():
            raise ValueError(f"manifest-listed shard is missing: {path}")
        size = path.stat().st_size
        if size != entry.size_bytes:
            raise ValueError(
                f"shard {path} is {size} bytes; manifest expects {entry.size_bytes}"
            )
        digest = sha256_file(path)
        if digest != entry.sha256:
            raise ValueError(
                f"shard {path} sha256 is {digest}; manifest expects {entry.sha256}"
            )
        paths.append(path)
    return paths


def publish_directory(staged: Path, target: Path) -> None:
    """Publish a verified sibling directory with rollback on rename failure.

    Portable filesystems cannot atomically replace a non-empty directory in
    one call.  This uses same-filesystem renames and retains the old directory
    until the new one is in place, restoring it if publication fails.
    """

    if staged.resolve() == target.resolve():
        raise ValueError("staged and target directories must be different")
    if staged.parent.resolve() != target.parent.resolve():
        raise ValueError("staged and target directories must be siblings")
    if staged.is_symlink() or not staged.is_dir():
        raise ValueError(f"staged directory does not exist: {staged}")
    if target.is_symlink():
        raise ValueError(f"publish target may not be a symlink: {target}")

    backup: Path | None = None
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"publish target is not a directory: {target}")
        backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
        target.rename(backup)
    try:
        staged.rename(target)
    except BaseException as publish_error:
        if backup is not None and backup.exists() and not target.exists():
            try:
                backup.rename(target)
            except BaseException as rollback_error:
                raise RuntimeError(
                    f"failed to publish {target} and rollback {backup}: "
                    f"{rollback_error}"
                ) from publish_error
        raise
    if backup is not None:
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            # The new directory is already committed. Reporting publication as
            # failed would prompt a dangerous retry even though readers see the
            # new bundle; retain the backup for manual cleanup instead.
            warnings.warn(
                f"published {target} but could not remove backup {backup}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
