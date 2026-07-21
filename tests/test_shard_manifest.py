import hashlib
import json
from pathlib import Path

import pytest

from registry_faces.shards import (
    parse_manifest,
    publish_directory,
    verified_shard_paths,
)


def _manifest(state: str, name: str, payload: bytes) -> dict:
    return {
        "stateCode": state,
        "version": "2026-07-21T00:00:00Z",
        "totalRecords": 1,
        "shards": [
            {
                "name": name,
                "sizeBytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "recordCount": 1,
            }
        ],
    }


def test_verified_shards_use_manifest_inventory_and_digest(tmp_path: Path):
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    payload = b"zip payload"
    (state_dir / "shard-000.zip").write_bytes(payload)
    (state_dir / "shard-999.zip").write_bytes(b"stale local object")
    (state_dir / "manifest.json").write_text(
        json.dumps(_manifest("US-XX", "shard-000.zip", payload)),
        encoding="utf-8",
    )

    assert verified_shard_paths(state_dir) == [state_dir / "shard-000.zip"]

    (state_dir / "shard-000.zip").write_bytes(b"same-ish but wrong")
    with pytest.raises(ValueError, match="bytes|sha256"):
        verified_shard_paths(state_dir)


def test_manifest_rejects_traversal_and_inconsistent_totals():
    payload = b"x"
    traversal = _manifest("US-XX", "../shard-000.zip", payload)
    with pytest.raises(ValueError, match="unsafe name"):
        parse_manifest(traversal)

    bad_total = _manifest("US-XX", "shard-000.zip", payload)
    bad_total["totalRecords"] = 2
    with pytest.raises(ValueError, match="shard total"):
        parse_manifest(bad_total)


def test_content_addressed_name_must_match_declared_digest():
    payload = b"x"
    manifest = _manifest(
        "US-XX", f"shard-000-{'0' * 64}.zip", payload
    )
    with pytest.raises(ValueError, match="does not match"):
        parse_manifest(manifest)


def test_manifest_rejects_empty_by_default():
    manifest = {
        "stateCode": "US-XX",
        "version": "v1",
        "totalRecords": 0,
        "shards": [],
    }
    with pytest.raises(ValueError, match="no shards"):
        parse_manifest(manifest)
    assert parse_manifest(manifest, require_nonempty=False).shards == ()


def test_publish_directory_replaces_only_after_staging(tmp_path: Path):
    target = tmp_path / "US-XX"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    staged = tmp_path / ".US-XX.build-test"
    staged.mkdir()
    (staged / "new.txt").write_text("new", encoding="utf-8")

    publish_directory(staged, target)

    assert not staged.exists()
    assert not (target / "old.txt").exists()
    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
