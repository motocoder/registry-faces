"""Photo subsystem — manifest format and sync logic.

`record.json` knows nothing about photos. The authoritative source for what
photos belong to a person is `photos/manifest.json` in that person's folder.

Adapters expose `extract_photos(raw) -> list[PhotoRef]`. The store turns
those refs into pending manifest entries. `sync_photos()` downloads pending
entries and fills in the file fields.

Invariant: every file in `photos/` has exactly one manifest entry, and every
entry with `local_filename` set points to an existing file. `verify` checks
this; `sync` maintains it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Models


class PhotoRef(BaseModel):
    """Minimal photo reference returned by `Adapter.extract_photos()`.

    Carries just what the adapter knows: a URL and where it came from.
    File-level fields (sha256, size, etc.) are filled in by sync_photos.
    """

    url: str
    source_type: str = "registry"
    source_name: str | None = None


class PhotoEntry(BaseModel):
    """A row in photos/manifest.json.

    Fields below `source_name` are populated by `sync_photos` after a
    successful download. A `local_filename` of None means pending.
    """

    model_config = ConfigDict(extra="allow")

    url: str
    source_type: str = "registry"
    source_name: str | None = None
    local_filename: str | None = None
    sha256: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    fetched_at: datetime | None = None


class PhotoManifest(BaseModel):
    person: dict
    last_synced_at: datetime | None = None
    photos: list[PhotoEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# I/O


def read_manifest(person_dir: Path) -> PhotoManifest | None:
    path = person_dir / "photos" / "manifest.json"
    if not path.exists():
        return None
    return PhotoManifest.model_validate_json(path.read_text())


def write_manifest(person_dir: Path, manifest: PhotoManifest) -> None:
    photos_dir = person_dir / "photos"
    photos_dir.mkdir(exist_ok=True)
    (photos_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n"
    )


# ---------------------------------------------------------------------------
# Merge


def merge_photo_refs(
    person_dir: Path,
    person_key: dict,
    refs: list[PhotoRef],
) -> PhotoManifest | None:
    """Merge a list of PhotoRef from the latest ingest into the manifest.

    Rules:
      - Dedup by URL.
      - New URLs append as pending entries (file fields all None).
      - Existing entries are never overwritten — sync fills them in later.
      - If `refs` is empty and no manifest exists, returns None (no manifest written).
    """
    existing = read_manifest(person_dir)
    if existing is None and not refs:
        return None

    if existing is None:
        existing = PhotoManifest(person=person_key)

    by_url = {e.url: e for e in existing.photos}
    for ref in refs:
        if ref.url in by_url:
            entry = by_url[ref.url]
            # Backfill source labels if previous entry left them blank
            if entry.source_name is None and ref.source_name is not None:
                entry.source_name = ref.source_name
            continue
        existing.photos.append(
            PhotoEntry(
                url=ref.url,
                source_type=ref.source_type,
                source_name=ref.source_name,
            )
        )

    write_manifest(person_dir, existing)
    return existing


# ---------------------------------------------------------------------------
# Sync


_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _ext_for(content_type: str | None, url: str) -> str:
    if content_type:
        primary = content_type.split(";", 1)[0].strip().lower()
        if primary in _CONTENT_TYPE_TO_EXT:
            return _CONTENT_TYPE_TO_EXT[primary]
    # Fallback to URL suffix
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return ".jpeg" if suffix == ".jpeg" else suffix
    return ".bin"


def _filename_for(index: int, entry: PhotoEntry, ext: str) -> str:
    label = entry.source_type or "photo"
    return f"{index:03d}-{label}{ext}"


def iter_person_dirs(records_root: Path, jurisdiction: str | None = None) -> Iterator[Path]:
    """Yield every per-person directory under records_root."""
    if not records_root.exists():
        return
    for jur_dir in sorted(records_root.iterdir()):
        if not jur_dir.is_dir():
            continue
        if jurisdiction and jur_dir.name != jurisdiction:
            continue
        for person_dir in sorted(jur_dir.iterdir()):
            if person_dir.is_dir():
                yield person_dir


def count_pending_photos(
    records_root: Path, jurisdiction: str | None = None, include_existing: bool = False
) -> int:
    """Count manifest entries that still need a download.

    If `include_existing` is True, count every photo entry instead — the value
    you'd want when running with --refresh.
    """
    total = 0
    for person_dir in iter_person_dirs(records_root, jurisdiction):
        manifest = read_manifest(person_dir)
        if manifest is None:
            continue
        for entry in manifest.photos:
            if include_existing or not entry.local_filename:
                total += 1
    return total


def sync_photos(
    records_root: Path,
    jurisdiction: str | None = None,
    refresh: bool = False,
    timeout: float = 60.0,
    user_agent: str = "registry-faces/0.1",
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict:
    """Walk per-person manifests and download any pending photo entries.

    Args:
        records_root: The `records/` directory under the registry root.
        jurisdiction: Restrict to one jurisdiction code.
        refresh: If True, re-download even entries that already have a local file.
        timeout: HTTP timeout per request.

    Returns:
        A summary dict: {"downloaded": int, "skipped": int, "failed": [(url, error)]}.
    """
    downloaded = 0
    skipped = 0
    failed: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc)
    total_pending = count_pending_photos(records_root, jurisdiction, include_existing=refresh)
    processed = 0
    if progress_callback is not None:
        progress_callback(processed, total_pending)

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": user_agent},
    ) as client:
        for person_dir in iter_person_dirs(records_root, jurisdiction):
            manifest = read_manifest(person_dir)
            if manifest is None:
                continue
            photos_dir = person_dir / "photos"
            photos_dir.mkdir(exist_ok=True)

            changed = False
            for idx, entry in enumerate(manifest.photos, start=1):
                if entry.local_filename and not refresh:
                    skipped += 1
                    continue
                try:
                    resp = client.get(entry.url)
                    resp.raise_for_status()
                except Exception as e:
                    failed.append((entry.url, f"{type(e).__name__}: {e}"))
                    processed += 1
                    if progress_callback is not None:
                        progress_callback(processed, total_pending)
                    continue

                content = resp.content
                content_type = resp.headers.get("content-type")
                ext = _ext_for(content_type, entry.url)
                sha = hashlib.sha256(content).hexdigest()

                # If refresh and content unchanged, just touch fetched_at.
                if entry.sha256 == sha and entry.local_filename:
                    entry.fetched_at = now
                    changed = True
                    skipped += 1
                    continue

                filename = _filename_for(idx, entry, ext)
                (photos_dir / filename).write_bytes(content)

                entry.local_filename = filename
                entry.sha256 = sha
                entry.content_type = (content_type or "").split(";", 1)[0].strip() or None
                entry.size_bytes = len(content)
                entry.fetched_at = now
                changed = True
                downloaded += 1
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total_pending)

            if changed:
                manifest.last_synced_at = now
                write_manifest(person_dir, manifest)

    return {"downloaded": downloaded, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Verify


def verify_person_photos(person_dir: Path) -> list[str]:
    """Check the photos/ folder vs the manifest for one person. Returns issues."""
    photos_dir = person_dir / "photos"
    if not photos_dir.exists():
        return []

    manifest = read_manifest(person_dir)
    if manifest is None:
        return [f"{photos_dir}: photos/ exists but no manifest.json"]

    issues: list[str] = []
    files_on_disk = {p.name for p in photos_dir.iterdir() if p.is_file() and p.name != "manifest.json"}
    manifest_files = {e.local_filename for e in manifest.photos if e.local_filename}

    for orphan in sorted(files_on_disk - manifest_files):
        issues.append(f"{photos_dir / orphan}: file on disk has no manifest entry")
    for missing in sorted(manifest_files - files_on_disk):
        issues.append(f"{photos_dir / missing}: manifest entry points to a missing file")
    return issues
