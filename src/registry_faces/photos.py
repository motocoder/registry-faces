"""Photo subsystem — registry-faces binding over ``web_scrubber.photos``.

The manifest format, sync engine, and verify logic now live in the shared
framework. registry-faces keeps only its ``PhotoRef`` default (``source_type``
= ``"registry"``, which lands in ``001-registry.jpg`` filenames) and the
historical ``sync_photos`` signature.
"""

from __future__ import annotations

from pathlib import Path

from web_scrubber.photos import (  # noqa: F401  (re-exported for adapters/tests)
    PhotoEntry,
    PhotoManifest,
    count_pending_photos,
    iter_person_dirs,
    merge_photo_refs,
    read_manifest,
    verify_person_photos,
    write_manifest,
)
from web_scrubber.photos import PhotoRef as _PhotoRef
from web_scrubber.photos import sync_photos as _sync_photos


class PhotoRef(_PhotoRef):
    """Minimal photo reference returned by ``Adapter.extract_photos()``."""

    source_type: str = "registry"


def sync_photos(
    records_root: Path,
    jurisdiction: str | None = None,
    refresh: bool = False,
    timeout: float = 60.0,
    user_agent: str = "registry-faces/0.1",
    progress_callback=None,
) -> dict:
    """Download pending photos. Same signature as historical registry-faces."""
    return _sync_photos(
        records_root,
        jurisdiction=jurisdiction,
        refresh=refresh,
        timeout=timeout,
        user_agent=user_agent,
        progress_callback=progress_callback,
    )
