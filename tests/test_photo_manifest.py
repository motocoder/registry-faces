"""Photo manifest merge + verify invariants."""

from pathlib import Path

from registry_faces.photos import (
    PhotoEntry,
    PhotoManifest,
    PhotoRef,
    count_pending_photos,
    merge_photo_refs,
    read_manifest,
    verify_person_photos,
    write_manifest,
)


PERSON_KEY = {"jurisdiction": "US-XX", "source_id": "1"}


def _person_dir(tmp_path: Path) -> Path:
    p = tmp_path / "records" / "US-XX" / "1"
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_merge_creates_manifest_when_none_exists(tmp_path: Path):
    pd = _person_dir(tmp_path)
    refs = [PhotoRef(url="https://example.com/a.jpg", source_type="registry")]
    manifest = merge_photo_refs(pd, PERSON_KEY, refs)
    assert manifest is not None
    assert len(manifest.photos) == 1
    assert manifest.photos[0].local_filename is None  # pending


def test_merge_no_manifest_and_no_refs_returns_none(tmp_path: Path):
    pd = _person_dir(tmp_path)
    assert merge_photo_refs(pd, PERSON_KEY, []) is None


def test_merge_appends_new_urls(tmp_path: Path):
    pd = _person_dir(tmp_path)
    merge_photo_refs(pd, PERSON_KEY, [PhotoRef(url="https://example.com/a.jpg")])
    merge_photo_refs(pd, PERSON_KEY, [PhotoRef(url="https://example.com/b.jpg")])
    final = read_manifest(pd)
    urls = [e.url for e in final.photos]
    assert urls == ["https://example.com/a.jpg", "https://example.com/b.jpg"]


def test_merge_preserves_existing_entry(tmp_path: Path):
    pd = _person_dir(tmp_path)
    # Seed a manifest with a "downloaded" entry
    seeded = PhotoManifest(
        person=PERSON_KEY,
        photos=[
            PhotoEntry(
                url="https://example.com/a.jpg",
                source_type="registry",
                local_filename="001-registry.jpg",
                sha256="deadbeef",
                size_bytes=1024,
            )
        ],
    )
    write_manifest(pd, seeded)

    # Re-merge with the same URL — should NOT overwrite the downloaded fields
    merge_photo_refs(pd, PERSON_KEY, [PhotoRef(url="https://example.com/a.jpg")])
    final = read_manifest(pd)
    assert len(final.photos) == 1
    assert final.photos[0].local_filename == "001-registry.jpg"
    assert final.photos[0].sha256 == "deadbeef"


def test_merge_backfills_missing_source_name(tmp_path: Path):
    pd = _person_dir(tmp_path)
    merge_photo_refs(pd, PERSON_KEY, [PhotoRef(url="https://example.com/a.jpg")])
    merge_photo_refs(
        pd,
        PERSON_KEY,
        [PhotoRef(url="https://example.com/a.jpg", source_name="State Registry")],
    )
    assert read_manifest(pd).photos[0].source_name == "State Registry"


def test_count_pending_photos(tmp_path: Path):
    records_root = tmp_path / "records"
    pd_a = records_root / "US-XX" / "1"
    pd_a.mkdir(parents=True)
    pd_b = records_root / "US-XX" / "2"
    pd_b.mkdir(parents=True)

    merge_photo_refs(pd_a, {**PERSON_KEY, "source_id": "1"}, [PhotoRef(url="http://x/a")])
    merge_photo_refs(pd_a, {**PERSON_KEY, "source_id": "1"}, [PhotoRef(url="http://x/b")])
    merge_photo_refs(pd_b, {**PERSON_KEY, "source_id": "2"}, [PhotoRef(url="http://x/c")])
    # Mark one as downloaded
    m = read_manifest(pd_a)
    m.photos[0].local_filename = "001-registry.jpg"
    write_manifest(pd_a, m)

    assert count_pending_photos(records_root, jurisdiction="US-XX") == 2  # b + c are pending
    assert count_pending_photos(records_root, jurisdiction="US-XX", include_existing=True) == 3


def test_verify_detects_orphan_file(tmp_path: Path):
    pd = _person_dir(tmp_path)
    merge_photo_refs(pd, PERSON_KEY, [PhotoRef(url="https://example.com/a.jpg")])
    # Drop a stray file in photos/
    (pd / "photos" / "stray.jpg").write_bytes(b"x")
    issues = verify_person_photos(pd)
    assert any("stray.jpg" in i for i in issues)


def test_verify_detects_missing_file(tmp_path: Path):
    pd = _person_dir(tmp_path)
    seeded = PhotoManifest(
        person=PERSON_KEY,
        photos=[
            PhotoEntry(
                url="https://example.com/a.jpg",
                local_filename="001-registry.jpg",
                sha256="deadbeef",
            )
        ],
    )
    write_manifest(pd, seeded)
    issues = verify_person_photos(pd)
    assert any("missing file" in i.lower() for i in issues)


def test_verify_clean_manifest(tmp_path: Path):
    pd = _person_dir(tmp_path)
    seeded = PhotoManifest(
        person=PERSON_KEY,
        photos=[
            PhotoEntry(
                url="https://example.com/a.jpg",
                local_filename="001-registry.jpg",
                sha256="deadbeef",
            )
        ],
    )
    write_manifest(pd, seeded)
    (pd / "photos" / "001-registry.jpg").write_bytes(b"x")
    assert verify_person_photos(pd) == []
