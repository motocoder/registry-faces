import hashlib
import importlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


pytest.importorskip("boto3")

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
download_shards = importlib.import_module("download_shards")
load_shards_identity = importlib.import_module("load_shards_identity")
upload_shards = importlib.import_module("upload_shards")


def _manifest(state: str, payload: bytes) -> bytes:
    return (json.dumps({
        "stateCode": state,
        "version": "v1",
        "totalRecords": 1,
        "shards": [{
            "name": "shard-000.zip",
            "sizeBytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "recordCount": 1,
        }],
    }) + "\n").encode()


class _Body:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class _DownloadClient:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def get_object(self, *, Bucket: str, Key: str):
        return {"Body": _Body(self.objects[Key])}

    def download_file(self, bucket: str, key: str, destination: str):
        Path(destination).write_bytes(self.objects[key])


class _UploadClient:
    def __init__(self, heads: dict[str, tuple[int, str]], fail: set[str] | None = None):
        self.heads = heads
        self.fail = fail or set()
        self.uploaded: list[str] = []

    def head_object(self, *, Bucket: str, Key: str):
        size, digest = self.heads[Key]
        return {"ContentLength": size, "Metadata": {"sha256": digest}}

    def upload_file(self, path: str, bucket: str, key: str, ExtraArgs: dict):
        if key in self.fail:
            raise RuntimeError("simulated upload failure")
        assert ExtraArgs["Metadata"]["sha256"] == hashlib.sha256(
            Path(path).read_bytes()
        ).hexdigest()
        self.uploaded.append(key)


def _upload_items(tmp_path: Path):
    shard = tmp_path / "shard-000.zip"
    manifest = tmp_path / "manifest.json"
    shard.write_bytes(b"shard")
    manifest.write_bytes(b"manifest")
    shard_digest = hashlib.sha256(b"shard").hexdigest()
    shard_item = upload_shards.UploadItem(
        shard,
        f"US-XX/shard-000-{shard_digest}.zip",
        shard_digest,
    )
    manifest_item = upload_shards.UploadItem(
        manifest,
        "US-XX/manifest.json",
        hashlib.sha256(b"manifest").hexdigest(),
        is_manifest=True,
    )
    return shard_item, manifest_item


def test_upload_does_not_skip_equal_size_different_content(tmp_path: Path):
    shard, _manifest_item = _upload_items(tmp_path)
    client = _UploadClient(
        {shard.rel: (shard.path.stat().st_size, "0" * 64)}
    )

    ok, _line = upload_shards._upload_one(
        client,
        "bucket",
        "",
        shard.path,
        shard.rel,
        1,
        1,
        False,
        checksum=shard.sha256,
    )

    assert ok is True
    assert client.uploaded == [shard.rel]


def test_upload_withholds_manifest_after_shard_failure(tmp_path: Path):
    shard, manifest = _upload_items(tmp_path)
    heads = {
        shard.rel: (0, "0" * 64),
        manifest.rel: (manifest.path.stat().st_size, manifest.sha256),
    }
    client = _UploadClient(heads, fail={shard.rel})

    _uploaded, _skipped, failed = upload_shards._publish_inventory(
        client=client,
        bucket="bucket",
        prefix="",
        shard_items=[shard],
        manifest_items=[manifest],
        parallelism=2,
        dry_run=False,
    )

    assert failed == 2
    assert manifest.rel not in client.uploaded


def test_upload_always_publishes_manifest_as_commit_marker(tmp_path: Path):
    shard, manifest = _upload_items(tmp_path)
    client = _UploadClient({
        shard.rel: (shard.path.stat().st_size, shard.sha256),
        manifest.rel: (manifest.path.stat().st_size, manifest.sha256),
    })

    uploaded, skipped, failed = upload_shards._publish_inventory(
        client=client,
        bucket="bucket",
        prefix="",
        shard_items=[shard],
        manifest_items=[manifest],
        parallelism=2,
        dry_run=False,
    )

    assert (uploaded, skipped, failed) == (1, 1, 0)
    assert client.uploaded == [manifest.rel]


def test_upload_refuses_mutable_legacy_shard_names(tmp_path: Path):
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    payload = b"legacy mutable object"
    (state_dir / "shard-000.zip").write_bytes(payload)
    (state_dir / "manifest.json").write_bytes(_manifest("US-XX", payload))

    with pytest.raises(ValueError, match="mutable legacy name"):
        upload_shards._publication_items(tmp_path)


def test_failed_new_upload_cannot_touch_old_manifest_objects(tmp_path: Path):
    shard, manifest = _upload_items(tmp_path)
    old_key = f"US-XX/shard-000-{'a' * 64}.zip"
    client = _UploadClient(
        {
            shard.rel: (0, "0" * 64),
            manifest.rel: (0, "0" * 64),
            old_key: (123, "a" * 64),
        },
        fail={shard.rel},
    )

    _uploaded, _skipped, failed = upload_shards._publish_inventory(
        client=client,
        bucket="bucket",
        prefix="",
        shard_items=[shard],
        manifest_items=[manifest],
        parallelism=1,
        dry_run=False,
    )

    assert failed == 2
    assert client.uploaded == []
    assert old_key not in client.uploaded


def test_download_uses_manifest_and_replaces_stale_local_state(tmp_path: Path):
    payload = b"remote shard"
    manifest = _manifest("US-XX", payload)
    client = _DownloadClient({
        "root/US-XX/manifest.json": manifest,
        "root/US-XX/shard-000.zip": payload,
    })
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    (state_dir / "shard-999.zip").write_bytes(b"stale")
    (state_dir / "old.txt").write_text("old", encoding="utf-8")

    downloaded, skipped, _bytes = download_shards._download_state(
        client,
        bucket="bucket",
        prefix="root/",
        state="US-XX",
        shards_dir=tmp_path,
        dry_run=False,
    )

    assert (downloaded, skipped) == (1, 0)
    assert (state_dir / "shard-000.zip").read_bytes() == payload
    assert not (state_dir / "shard-999.zip").exists()
    assert not (state_dir / "old.txt").exists()


def test_bad_download_preserves_last_good_local_state(tmp_path: Path):
    expected = b"expected"
    manifest = _manifest("US-XX", expected)
    client = _DownloadClient({
        "root/US-XX/manifest.json": manifest,
        "root/US-XX/shard-000.zip": b"corrupt",
    })
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    (state_dir / "last-good.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest expects|sha256"):
        download_shards._download_state(
            client,
            bucket="bucket",
            prefix="root/",
            state="US-XX",
            shards_dir=tmp_path,
            dry_run=False,
        )

    assert (state_dir / "last-good.txt").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".US-XX.download-*"))


def test_download_dry_run_creates_no_local_directories(tmp_path: Path):
    payload = b"remote shard"
    manifest = _manifest("US-XX", payload)
    client = _DownloadClient({
        "root/US-XX/manifest.json": manifest,
        "root/US-XX/shard-000.zip": payload,
    })
    destination = tmp_path / "not-created"

    assert download_shards._download_state(
        client,
        bucket="bucket",
        prefix="root/",
        state="US-XX",
        shards_dir=destination,
        dry_run=True,
    ) == (0, 0, 0)
    assert not destination.exists()


def test_identity_loader_fails_state_without_committed_manifest(tmp_path: Path):
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    (state_dir / "shard-000.zip").write_bytes(b"uncommitted")

    code = load_shards_identity.main(
        ["--state", "US-XX", "--shards-dir", str(tmp_path), "--dry-run"]
    )

    assert code == 1


def test_identity_loader_accepts_verified_manifest_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = b"verified shard payload"
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    (state_dir / "shard-000.zip").write_bytes(payload)
    (state_dir / "manifest.json").write_bytes(_manifest("US-XX", payload))
    seen: list[tuple[str, list[Path], int | None]] = []

    def fake_dry_run(state: str, zips: list[Path], limit: int | None):
        seen.append((state, zips, limit))
        return {
            "state": state,
            "records": 1,
            "new_persons": 0,
            "photos": 0,
            "errors": 0,
            "samples": [],
        }

    monkeypatch.setattr(load_shards_identity, "dry_run_state", fake_dry_run)

    code = load_shards_identity.main(
        ["--state", "US-XX", "--shards-dir", str(tmp_path), "--dry-run"]
    )

    assert code == 0
    assert seen == [("US-XX", [state_dir / "shard-000.zip"], None)]


def test_identity_verification_reports_missing_source_index():
    class Store:
        @staticmethod
        def lookup_source(_jurisdiction, _source_id):
            return None

    class Bundle:
        store = Store()

    assert (
        load_shards_identity.verify(
            Bundle(), "US-XX", [("US-XX", "missing", "Missing Person", 0)]
        )
        is False
    )


def test_identity_loader_rejects_cross_state_zip_entries(tmp_path: Path):
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    shard = state_dir / "shard-000.zip"
    with zipfile.ZipFile(shard, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("US-YY/person-1/record.json", b"{}")

    with pytest.raises(ValueError, match="cross-state"):
        list(load_shards_identity.iter_persons(shard))


def test_identity_loader_rejects_compressed_zip_bombs(tmp_path: Path):
    state_dir = tmp_path / "US-XX"
    state_dir.mkdir()
    shard = state_dir / "shard-000.zip"
    with zipfile.ZipFile(shard, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("US-XX/person-1/record.json", b"{}" * 1000)

    with pytest.raises(ValueError, match="compressed ZIP entry"):
        list(load_shards_identity.iter_persons(shard))


@pytest.mark.parametrize("force_unlock", [False, True])
def test_identity_loader_honors_force_unlock_flag(force_unlock: bool):
    seen = {}
    sentinel = object()

    def build(_cfg, **kwargs):
        seen.update(kwargs)
        return sentinel

    result = load_shards_identity._open_bundle(
        object(),
        "US-XX",
        SimpleNamespace(force_unlock=force_unlock),
        build,
    )

    assert result is sentinel
    assert seen["force_unlock"] is force_unlock


def test_identity_loader_rejects_blacklisted_state(tmp_path: Path):
    assert load_shards_identity.main([
        "--state", "US-NY", "--shards-dir", str(tmp_path), "--dry-run"
    ]) == 1


def test_identity_bundle_close_failure_is_not_silently_successful():
    class FailingBlobs:
        @staticmethod
        def close():
            raise RuntimeError("flush failed")

    class Bundle:
        blobs = FailingBlobs()

        @staticmethod
        def close():
            return None

    with pytest.raises(RuntimeError, match="flush failed"):
        load_shards_identity._close_bundle(Bundle())
    load_shards_identity._close_bundle(Bundle(), suppress_errors=True)


def test_bulk_limit_counts_attempts_not_only_successes(monkeypatch: pytest.MonkeyPatch):
    people = [
        ("bad", b"bad", []),
        ("would-have-been-loaded", b"good", []),
    ]
    attempted: list[bytes] = []

    monkeypatch.setattr(
        load_shards_identity, "iter_all_persons", lambda _zips: iter(people)
    )

    def fail_first(_bundle, _row, rec_bytes, _photos, _refresh, **_kwargs):
        attempted.append(rec_bytes)
        raise ValueError("bad record")

    monkeypatch.setattr(load_shards_identity, "_ingest_one", fail_first)
    row = load_shards_identity.bulk_load_one_state(
        object(), "US-XX", [], limit=1, refresh=False
    )

    assert attempted == [b"bad"]
    assert row["records"] == 0
    assert row["errors"] == 1
