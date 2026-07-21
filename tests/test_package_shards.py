import importlib
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest


pytest.importorskip("PIL")

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
package_shards = importlib.import_module("package_shards")


def _record(records_root: Path, state: str = "US-XX") -> None:
    person_dir = records_root / state / "person-1"
    person_dir.mkdir(parents=True)
    (person_dir / "record.json").write_text(
        json.dumps({
            "source": {"jurisdiction": state},
            "identity": {"guid": "guid-1"},
            "value": "kept verbatim",
        }),
        encoding="utf-8",
    )


def _process(records: Path, shards: Path, **overrides) -> bool:
    options = {
        "state_code": "US-XX",
        "records_root": records,
        "out_root": shards,
        "shard_size_bytes": 1024 * 1024,
        "max_edge": 2560,
        "quality": 85,
        "version": "v1",
        "verify": True,
    }
    options.update(overrides)
    return package_shards.process_state(**options)


def test_empty_build_preserves_last_good_bundle(tmp_path: Path):
    records = tmp_path / "records"
    (records / "US-XX").mkdir(parents=True)
    shards = tmp_path / "shards"
    old = shards / "US-XX"
    old.mkdir(parents=True)
    (old / "last-good.txt").write_text("keep", encoding="utf-8")

    assert _process(records, shards) is False
    assert (old / "last-good.txt").read_text(encoding="utf-8") == "keep"


def test_verified_build_replaces_old_bundle_and_writes_manifest_last(tmp_path: Path):
    records = tmp_path / "records"
    _record(records)
    shards = tmp_path / "shards"
    old = shards / "US-XX"
    old.mkdir(parents=True)
    (old / "last-good.txt").write_text("old", encoding="utf-8")

    assert _process(records, shards) is True

    state_dir = shards / "US-XX"
    assert not (state_dir / "last-good.txt").exists()
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["totalRecords"] == 1
    name = manifest["shards"][0]["name"]
    assert re.fullmatch(r"shard-000-[0-9a-f]{64}\.zip", name)
    assert name == f"shard-000-{manifest['shards'][0]['sha256']}.zip"
    first_payload = (state_dir / name).read_bytes()
    with zipfile.ZipFile(state_dir / name) as archive:
        assert {entry.date_time for entry in archive.infolist()} == {
            (1980, 1, 1, 0, 0, 0)
        }

    # Content addressing only works as an upload optimization if identical
    # inputs produce byte-identical archives on successive builds.
    assert _process(records, shards, version="v2") is True
    rebuilt = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert rebuilt["shards"][0]["name"] == name
    assert (state_dir / name).read_bytes() == first_payload


def test_failed_verification_preserves_last_good_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    records = tmp_path / "records"
    _record(records)
    shards = tmp_path / "shards"
    old = shards / "US-XX"
    old.mkdir(parents=True)
    (old / "last-good.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(package_shards, "verify_shards", lambda *_args: False)

    assert _process(records, shards) is False
    assert (old / "last-good.txt").read_text(encoding="utf-8") == "keep"
    assert not list(shards.glob(".US-XX.build-*"))


def test_scan_metadata_retains_path_not_record_payload(tmp_path: Path):
    records = tmp_path / "records"
    _record(records)
    user_dir = records / "US-XX" / "person-1"

    meta = package_shards.scan_meta(user_dir, package_shards.Stats())

    assert meta is not None
    assert meta.record_path == user_dir / "record.json"
    assert not hasattr(meta, "record_json_bytes")


def test_missing_requested_state_fails_and_preserves_old_bundle(tmp_path: Path):
    records = tmp_path / "records"
    records.mkdir()
    shards = tmp_path / "shards"
    old = shards / "US-XX"
    old.mkdir(parents=True)
    (old / "last-good.txt").write_text("keep", encoding="utf-8")

    assert _process(records, shards) is False
    assert (old / "last-good.txt").read_text(encoding="utf-8") == "keep"
