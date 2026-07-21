"""Unpack downloaded shards and publish them into the centralized person identity.

Reads manifest-listed ``shards/US-XX/shard-*.zip`` files (see
``download_shards.py``), reconstructs
each ``OffenderRecord`` from the bundled ``record.json``, maps it onto the
canonical Person/RegistryAttachment, and ingests it into the IdentityService.

The win over re-running adapters: shards already carry the photo BYTES, so each
image goes straight into the BlobStore via ``blobs.put()`` (content-addressed
sha256 -> ``blob_key``) with NO re-download from the source registry. Shards
dropped the photo manifest, so each photo is given a stable synthetic URL
(``shard://<jur>/<sid>/<filename>``) for dedup; the functional pointer is the
``blob_key``.

Per-state HBase lock is ``registry:<jurisdiction>`` (same as a live
``ingest-identity <state>``), so a state can't double-run but states are
independent — and it never touches the global ``identity`` lock.

Usage:
  # validate first — map only, no writes:
  .venv/Scripts/python.exe scripts/load_shards_identity.py --state US-MN --dry-run
  # push a few + read them back from HBase:
  .venv/Scripts/python.exe scripts/load_shards_identity.py --state US-MN --to hbase --limit 5 --verify
  # full state, then everything downloaded:
  .venv/Scripts/python.exe scripts/load_shards_identity.py --state US-MN --to hbase
  .venv/Scripts/python.exe scripts/load_shards_identity.py --all --to hbase
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from registry_faces.blacklist import BLACKLIST
from registry_faces.identity_map import map_item
from registry_faces.schema import OffenderRecord
from registry_faces.shards import verified_shard_paths


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConnectionLost(Exception):
    """Raised mid-state when the HBase Thrift connection drops, carrying the
    record index to resume from after a reconnect."""

    def __init__(self, index: int) -> None:
        super().__init__(f"connection lost at record {index}")
        self.index = index


_CONN_MARKERS = (
    "TTransportException", "TSocket", "ConnectionRefused", "ConnectionAborted",
    "ConnectionReset", "BrokenPipe", "Could not connect", "read 0 bytes",
    "10053", "10054", "10061", "Errno 32", "timed out",
)


def _is_conn_error(e: BaseException) -> bool:
    """True if an exception looks like a transient HBase/Thrift transport drop
    (worth a reconnect) rather than a data/mapping bug (per-record, not retried)."""
    s = f"{type(e).__module__}.{type(e).__name__}: {e}"
    return any(m in s for m in _CONN_MARKERS)


def _ctype(filename: str, data: bytes = b"") -> str:
    """Content type from the bytes' magic number first (shards store photos as
    ``*.bin``, so the extension is useless), falling back to the extension."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    n = filename.lower()
    if n.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if n.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


def iter_persons(zip_path: Path):
    """Yield (userdir, record_bytes, [(filename, photo_bytes), ...]) per person
    in one shard zip. Entry layout: ``US-XX/<userdir>/record.json`` and
    ``US-XX/<userdir>/photos/<filename>``."""
    state = zip_path.parent.name
    with zipfile.ZipFile(zip_path) as zf:
        groups = _validated_zip_groups(zf, state=state)
        for userdir, g in groups.items():
            rec_bytes = zf.read(g["record"])
            photos = [
                (info.filename.split("/")[-1], zf.read(info))
                for info in sorted(g["photos"], key=lambda item: item.filename)
            ]
            yield userdir, rec_bytes, photos


_MAX_ZIP_ENTRY_BYTES = 512 * 1024 * 1024
_MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def _safe_segment(value: str) -> bool:
    return bool(
        value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and all(ord(char) >= 32 for char in value)
    )


def _validated_zip_groups(zf: zipfile.ZipFile, *, state: str) -> dict[str, dict]:
    """Validate the exact package layout before reading entry payloads."""

    if re.fullmatch(r"US-[A-Z]{2}", state) is None:
        raise ValueError(f"invalid shard state directory {state!r}")
    groups: dict[str, dict] = {}
    seen_names: set[str] = set()
    total_size = 0
    for info in zf.infolist():
        name = info.filename
        if name in seen_names:
            raise ValueError(f"duplicate ZIP entry {name!r}")
        seen_names.add(name)
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise ValueError(f"encrypted ZIP entry is not supported: {name!r}")
        if info.compress_type != zipfile.ZIP_STORED:
            raise ValueError(f"compressed ZIP entry is not package output: {name!r}")
        if info.file_size < 0 or info.file_size > _MAX_ZIP_ENTRY_BYTES:
            raise ValueError(f"ZIP entry is too large: {name!r}")
        total_size += info.file_size
        if total_size > _MAX_ZIP_TOTAL_BYTES:
            raise ValueError("ZIP expands beyond the loader safety limit")

        parts = name.split("/")
        if any(not _safe_segment(part) for part in parts):
            raise ValueError(f"unsafe ZIP entry name {name!r}")
        if len(parts) == 3 and parts[0] == state and parts[2] == "record.json":
            userdir = parts[1]
            group = groups.setdefault(userdir, {"record": None, "photos": []})
            if group["record"] is not None:
                raise ValueError(f"duplicate record for {state}/{userdir}")
            group["record"] = info
        elif len(parts) == 4 and parts[0] == state and parts[2] == "photos":
            userdir = parts[1]
            group = groups.setdefault(userdir, {"record": None, "photos": []})
            if any(
                old.filename.split("/")[-1] == parts[3]
                for old in group["photos"]
            ):
                raise ValueError(f"duplicate photo for {state}/{userdir}: {parts[3]}")
            group["photos"].append(info)
        else:
            raise ValueError(f"unexpected or cross-state ZIP entry {name!r}")

    if not groups:
        raise ValueError("shard ZIP contains no records")
    missing = [userdir for userdir, group in groups.items() if group["record"] is None]
    if missing:
        raise ValueError(f"photo entries have no record.json for {missing[0]!r}")
    return groups


def push_photos(bundle, uuid: str, jur: str, sid: str, photos: list, refresh: bool,
                meta_only: bool = False) -> int:
    """Record each photo on the person's manifest. ``meta_only`` (phase 1) writes
    the pointer (``blob_key`` = sha256 of the bytes) WITHOUT pushing bytes — the
    bytes go up later, lock-free, in the photo phase. Otherwise pushes bytes now."""
    from web_scrubber.person.models import PersonPhotos, PhotoEntry

    if not photos:
        return 0
    pp = bundle.store.get_photos(uuid) or PersonPhotos(person_uuid=uuid)
    by_url = {e.url: e for e in pp.photos}
    added = 0
    for filename, data in photos:
        url = f"shard://{jur}/{sid}/{filename}"
        entry = by_url.get(url)
        key = hashlib.sha256(data).hexdigest()  # = blob_key (content address)
        if entry and entry.blob_key == key and not refresh:
            continue
        loc = None
        if not meta_only:  # push the bytes now (non-two-phase path)
            loc = bundle.blobs.put_located(data)  # sha256 + HDFS byte locator
            key = loc.key
        if entry is None:
            entry = PhotoEntry(
                url=url, source_jurisdiction=jur, source_id=sid,
                domain="registry", source_type="registry",
            )
            pp.photos.append(entry)
            by_url[url] = entry
        entry.blob_key = key
        entry.sha256 = key
        if loc is not None and loc.container is not None:  # single-phase: record now
            entry.blob_container = loc.container           # (two-phase fills these later)
            entry.blob_offset = loc.offset
            entry.blob_length = loc.length
        entry.content_type = _ctype(filename, data)
        entry.size_bytes = len(data)
        entry.fetched_at = _now()
        added += 1
    if added:
        bundle.store.put_photos(pp)
    return added


def _add_sample(row: dict, jur: str, sid: str, name: str, nph: int) -> None:
    """Keep up to 3 samples for --verify, preferring photo-bearing records so
    the blob path actually gets exercised."""
    samples = row["samples"]
    s = (jur, sid, name, nph)
    if nph > 0 and sum(1 for x in samples if x[3] > 0) < 3:
        samples.insert(0, s)  # photo samples to the front
    elif len(samples) < 3:
        samples.append(s)
    del samples[3:]


def iter_all_persons(zips: list[Path]):
    """Single forward-only pass over every person across all of a state's shards."""
    for zip_path in zips:
        yield from iter_persons(zip_path)


def iter_all_photo_bytes(zips: list[Path], limit: int | None = None):
    """Yield photo bytes for at most ``limit`` persons across one state."""

    persons = 0
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            groups = _validated_zip_groups(zf, state=zip_path.parent.name)
            for group in groups.values():
                if limit is not None and persons >= limit:
                    return
                persons += 1
                for info in sorted(group["photos"], key=lambda item: item.filename):
                    yield zf.read(info)


def _state_zips(shards_dir: Path, state: str) -> list[Path]:
    """Return the exact, digest-verified shard list committed by the manifest."""

    return verified_shard_paths(shards_dir / state, expected_state=state)


def run_photo_phase(
    states: list[str],
    shards_dir: Path,
    cfg,
    *,
    force_unlock: bool = False,
    limit: int | None = None,
) -> int:
    """PHASE 2: push every shard photo's bytes into the HDFS blob store (lock-free),
    then record each byte's (container, offset, length) onto its manifest in a brief
    locked pass so registry-server can range-read it via HttpFS. Phase 1 wrote the
    manifest pointers (blob_key = sha256); the byte push is content-addressed (dedups
    within the run, safe to re-run). Returns photos pushed."""
    from web_scrubber.person.hdfs_blob import HdfsAvroBlobStore

    blobs = HdfsAvroBlobStore(
        label="registry-faces",
        staging_root=cfg.blobs_staging_root,
        hdfs_base=cfg.blobs_hdfs_base,
        ssh_host=cfg.blobs_ssh_host,
    )
    total = 0
    mb = 0.0
    try:
        for state in states:
            zips = _state_zips(shards_dir, state)
            n = 0
            for data in iter_all_photo_bytes(zips, limit=limit):
                blobs.put(data)  # async push to HDFS; dedups by sha256
                n += 1
                mb += len(data) / 1024 / 1024
            total += n
            print(f"[{state}] photos queued: {n}  (cumulative {total}, {mb:.0f} MB)", flush=True)
    finally:
        print("waiting for final HDFS pushes ...", flush=True)
        blobs.close()
    print(f"PHOTO PHASE DONE: {total} photos, {mb:.0f} MB to HDFS", flush=True)

    # Phase 1 wrote the manifest pointers before these bytes existed, so the byte
    # offsets weren't known then. Now that the bytes are in HDFS, record each
    # photo's (container, offset, length) into its manifest from the locations the
    # blob store captured — so registry-server can range-read it via HttpFS.
    locs = blobs.locations()
    if cfg is not None and locs:
        from web_scrubber.person.config import build_identity_service
        from web_scrubber.person.photosync import apply_blob_locations
        print(f"recording blob offsets into manifests ({len(locs)} images) ...", flush=True)
        ob = build_identity_service(
            cfg, force_unlock=force_unlock,
            lock_owner="registry-faces:load-shards:offsets", lock_key="identity",
        )
        try:
            np_, nph_ = apply_blob_locations(
                ob.store, locs,
                progress=lambda n: (n % 20000 == 0) and print(f"  offsets: {n} persons", flush=True),
            )
            print(f"OFFSETS RECORDED: {nph_} photos across {np_} persons", flush=True)
        finally:
            ob.close()
    return total


def dry_run_state(state: str, zips: list[Path], limit: int | None) -> dict:
    """--dry-run: reconstruct + map every record, no writes. Validates the data."""
    row = {"state": state, "records": 0, "new_persons": 0, "photos": 0,
           "errors": 0, "samples": [], "reconnects": 0, "recycles": 0}
    attempted = 0
    for userdir, rec_bytes, photos in iter_all_persons(zips):
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        try:
            rec = OffenderRecord.model_validate_json(rec_bytes)
            if rec.source.jurisdiction != state:
                raise ValueError(
                    f"record jurisdiction {rec.source.jurisdiction!r} "
                    f"does not match shard state {state!r}"
                )
            map_item((rec, []))
            row["records"] += 1
            _add_sample(row, rec.source.jurisdiction, rec.source.source_id,
                        rec.identity.full_name, len(photos))
        except Exception:
            row["errors"] += 1
            if row["errors"] <= 3:
                print(f"  ERROR {state}/{userdir}:\n{traceback.format_exc(limit=3)}", flush=True)
    return row


def _ingest_one(
    bundle,
    row: dict,
    rec_bytes: bytes,
    photos: list,
    refresh: bool,
    *,
    expected_state: str,
    meta_only: bool = False,
) -> None:
    """Ingest one reconstructed record + its photo manifest. ``meta_only`` records
    photo pointers without pushing bytes (phase 1). Raises ConnectionLost on a
    transport drop (caller reopens + retries the same record); any other error is
    a per-record data error that the caller counts and skips."""
    rec = OffenderRecord.model_validate_json(rec_bytes)
    jur, sid = rec.source.jurisdiction, rec.source.source_id
    if jur != expected_state:
        raise ValueError(
            f"record jurisdiction {jur!r} does not match shard state "
            f"{expected_state!r}"
        )
    person, attachment, _ = map_item((rec, []))  # photos handled directly below
    try:
        result = bundle.service.ingest(person, attachment, [])
        row["photos"] += push_photos(bundle, result.person_uuid, jur, sid, photos,
                                     refresh, meta_only=meta_only)
        if result.is_new_person:
            row["new_persons"] += 1
    except Exception as e:
        if _is_conn_error(e):
            raise ConnectionLost(0) from e
        raise
    _add_sample(row, jur, sid, rec.identity.full_name, len(photos))


def _open_bundle(cfg, state: str, args, build_identity_service, max_stalls: int = 6):
    """Open an identity bundle, retrying transient connect failures with backoff."""
    stalls = 0
    while True:
        try:
            return build_identity_service(
                cfg, lock_owner=f"registry-faces:load-shards:{state}",
                lock_key=f"registry:{state}",
                force_unlock=bool(args.force_unlock),
            )
        except Exception as e:
            if _is_conn_error(e):
                stalls += 1
                if stalls > max_stalls:
                    raise
                wait = min(60, 5 * 2 ** min(stalls - 1, 4))
                print(f"[{state}] connect failed (stall {stalls}/{max_stalls}): "
                      f"{type(e).__name__}; retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise


def _close_bundle(bundle, *, suppress_errors: bool = False) -> None:
    """Clean teardown: flush photo bytes to HDFS FIRST (so a dead Thrift socket
    can't skip it and orphan blob_key pointers in HBase), then close store/lock/conn."""
    if bundle is None:
        return
    errors: list[Exception] = []
    try:
        bundle.blobs.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    try:
        bundle.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    if errors and not suppress_errors:
        if len(errors) > 1 and hasattr(errors[0], "add_note"):
            errors[0].add_note(f"bundle close also failed: {errors[1]}")
        raise errors[0]


def load_state(cfg, state: str, zips: list[Path], args, build_identity_service,
               recycle_every: int = 0, recycle_seconds: float = 0.0,
               max_stalls: int = 6) -> dict:
    """Load one state in a single forward-only pass over ONE long-lived connection.

    Proactive recycling is OFF by default (``recycle_*`` == 0) — the connection is
    meant to stay up, which requires the server's Thrift read-timeout to be raised
    past our idle gaps (``hbase.thrift.server.socket.read.timeout``). If you DO want
    periodic recycling, pass ``recycle_every`` / ``recycle_seconds`` > 0.

    A genuine unexpected drop is still caught and the same record retried after a
    reopen; ``max_stalls`` consecutive no-progress drops give up the state."""
    row = {"state": state, "records": 0, "new_persons": 0, "photos": 0,
           "errors": 0, "samples": [], "reconnects": 0, "recycles": 0}
    persons = iter_all_persons(zips)
    bundle = _open_bundle(cfg, state, args, build_identity_service, max_stalls)
    since = 0
    last_recycle = time.monotonic()
    pending = None      # record held for retry after an unexpected drop
    stalls = 0
    last_ok = time.monotonic()
    try:
        while True:
            if pending is None:
                try:
                    userdir, rec_bytes, photos = next(persons)
                except StopIteration:
                    break
            else:
                userdir, rec_bytes, photos = pending
            t_rec = time.monotonic()
            recycle_due = False
            try:
                _ingest_one(
                    bundle,
                    row,
                    rec_bytes,
                    photos,
                    args.refresh,
                    expected_state=state,
                )
                pending = None
                stalls = 0
                row["records"] += 1
                since += 1
                last_ok = time.monotonic()
                if args.limit and row["records"] >= args.limit:
                    break
                # Optional proactive recycle (off by default; relies on a raised
                # server read-timeout to keep one connection alive instead).
                recycle_due = (recycle_every > 0 and since >= recycle_every) or (
                    recycle_seconds > 0 and (time.monotonic() - last_recycle) >= recycle_seconds
                )
            except ConnectionLost:
                now = time.monotonic()
                print(f"[{state}] DROP at record {row['records']}: "
                      f"idle_since_last_ok={now - last_ok:.1f}s "
                      f"failing_op_took={now - t_rec:.1f}s", flush=True)
                # Unexpected mid-record drop — reopen and retry this same record.
                pending = (userdir, rec_bytes, photos)
                row["reconnects"] += 1
                stalls += 1
                if stalls > max_stalls:
                    print(f"[{state}] {max_stalls} consecutive drops, no progress; giving up "
                          f"({row['records']} loaded)", flush=True)
                    raise
                _close_bundle(bundle, suppress_errors=True)
                time.sleep(0.5)
                bundle = _open_bundle(cfg, state, args, build_identity_service, max_stalls)
                since = 0
                last_recycle = time.monotonic()
                last_ok = time.monotonic()
            except Exception:
                row["errors"] += 1
                if row["errors"] <= 3:
                    print(f"  ERROR {state}/{userdir}:\n{traceback.format_exc(limit=3)}", flush=True)
                pending = None
            if recycle_due:
                # Teardown/reopen is operational state, not a bad input record.
                # Let failures escape so the state cannot be reported successful
                # while writes or blob flushes remain uncertain.
                _close_bundle(bundle)
                bundle = _open_bundle(
                    cfg, state, args, build_identity_service, max_stalls
                )
                row["recycles"] += 1
                since = 0
                last_recycle = time.monotonic()
        if args.verify:
            try:
                if not verify(bundle, state, row["samples"]):
                    row["errors"] += 1
                    row["failed"] = True
            except Exception:  # noqa: BLE001 — verification fails closed
                row["errors"] += 1
                row["failed"] = True
    finally:
        _close_bundle(bundle)
    return row


def bulk_load_one_state(bundle, state: str, zips: list[Path], limit: int | None,
                        refresh: bool, meta_only: bool = False) -> dict:
    """Process one state's records through a SHARED bulk bundle. ``meta_only``
    (phase 1) records photo pointers without pushing bytes. The bulk store mirrors
    the indexes in RAM (reads never round-trip) and batches writes."""
    row = {"state": state, "records": 0, "new_persons": 0, "photos": 0,
           "errors": 0, "samples": [], "reconnects": 0, "recycles": 0}
    attempted = 0
    for userdir, rec_bytes, photos in iter_all_persons(zips):
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        try:
            _ingest_one(
                bundle,
                row,
                rec_bytes,
                photos,
                refresh,
                expected_state=state,
                meta_only=meta_only,
            )
            row["records"] += 1
        except ConnectionLost:
            raise  # dead connection — let the caller rebuild + re-stream this state
        except Exception:
            row["errors"] += 1
            if row["errors"] <= 3:
                print(f"  ERROR {state}/{userdir}:\n{traceback.format_exc(limit=3)}", flush=True)
    return row


def verify(bundle, state: str, samples: list) -> bool:
    """Read pushed persons back and return whether every sample is complete."""

    verified = True
    print(f"\n--- verify {state}: reading {len(samples)} persons back from the store ---")
    for jur, sid, name, nphotos in samples:
        uuid = bundle.store.lookup_source(jur, sid)
        if not uuid:
            print(f"  [{jur}/{sid}] NOT FOUND by source index — ingest did not index it!")
            verified = False
            continue
        person = bundle.store.get_person(uuid)
        if person is None:
            print(f"  [{jur}/{sid}] source index points to missing person {uuid}")
            verified = False
            continue
        atts = [
            attachment
            for attachment in bundle.store.iter_attachments(uuid)
            if getattr(attachment.source, "jurisdiction", None) == jur
            and getattr(attachment.source, "source_id", None) == sid
        ]
        pp = bundle.store.get_photos(uuid)
        photos = [
            photo
            for photo in (pp.photos if pp else [])
            if getattr(photo, "source_jurisdiction", None) == jur
            and getattr(photo, "source_id", None) == sid
        ]
        print(f"  uuid={uuid}")
        print(f"    person.full_name = {person.full_name!r}  sex={person.sex} "
              f"yob={person.year_of_birth} source_count={person.source_count}")
        print(f"    attachments={len(atts)}  (expected 1 for this source)")
        if atts:
            a = atts[0]
            naddr = len(getattr(a, 'addresses', []) or [])
            noff = len(getattr(a, 'offenses', []) or [])
            print(f"    attachment.source = {a.source.jurisdiction}/{a.source.source_id}  "
                  f"addresses={naddr} offenses={noff} raw={'yes' if a.raw else 'no'}")
        print(f"    photos: manifest={len(photos)} (shard had {nphotos})")
        if not atts or len(photos) < nphotos:
            verified = False
        for e in photos[:2]:
            photo_status = "?"
            try:
                photo_status = (
                    "bytes-ok"
                    if (e.blob_key and bundle.blobs.exists(e.blob_key))
                    else "MISSING"
                )
            except Exception as ex:  # noqa: BLE001
                photo_status = f"check-failed({type(ex).__name__})"
            print(f"      - {e.url}  blob_key={(e.blob_key or '')[:12]}.. "
                  f"size={e.size_bytes} {photo_status}")
            if photo_status != "bytes-ok":
                verified = False
        for entry in photos:
            try:
                if not entry.blob_key or not bundle.blobs.exists(entry.blob_key):
                    verified = False
            except Exception:  # noqa: BLE001 - verification must fail closed
                verified = False
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shards-dir", type=Path, default=Path("shards"))
    parser.add_argument("--state", action="append", default=None, help="US-XX, repeatable.")
    parser.add_argument("--all", action="store_true", help="Every US-* dir under shards-dir.")
    parser.add_argument("--to", choices=["file", "hbase"], default=None,
                        help="Override identity.mode (default from identity.properties).")
    parser.add_argument("--config", default="identity.properties")
    parser.add_argument("--limit", type=int, default=None, help="Max records per state.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Reconstruct + map only; no HBase/blob writes.")
    parser.add_argument("--refresh", action="store_true", help="Re-put photo bytes already stored.")
    parser.add_argument("--verify", action="store_true",
                        help="After loading each state, read a few persons back and print them.")
    parser.add_argument("--force-unlock", action="store_true")
    parser.add_argument("--bulk", action="store_true",
                        help="Fast one-shot backfill: ONE connection for the whole run, the "
                        "global 'identity' lock (sole writer — no concurrent ingests anywhere), "
                        "and an in-RAM index so reads never round-trip and writes batch. ~10x+ "
                        "over a tunnel. Seeds its cache from HBase at startup (idempotent). Runs "
                        "two phases: records (locked) then photo bytes (lock-free).")
    parser.add_argument("--records-only", action="store_true",
                        help="Bulk phase 1 only: load records + photo pointers, skip photo bytes.")
    parser.add_argument("--photos-only", action="store_true",
                        help="Phase 2 only (lock-free, no HBase): push shard photo bytes to HDFS.")
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.all and args.state:
        parser.error("--all and --state are mutually exclusive")
    if args.records_only and not args.bulk:
        parser.error("--records-only requires --bulk")
    if args.photos_only and (args.bulk or args.records_only):
        parser.error("--photos-only cannot be combined with --bulk or --records-only")
    if args.photos_only and args.dry_run:
        parser.error("--photos-only cannot be combined with --dry-run")
    if args.verify and (args.dry_run or args.photos_only):
        parser.error("--verify requires a record-loading mode")

    if args.all:
        if not args.shards_dir.is_dir():
            parser.error(f"shards dir not found: {args.shards_dir}")
        states = sorted(p.name for p in args.shards_dir.iterdir()
                        if p.is_dir() and p.name.startswith("US-"))
    elif args.state:
        states = list(dict.fromkeys(s.upper() for s in args.state))
    else:
        parser.error("pass --state US-XX (repeatable) or --all")
    if not states:
        parser.error(f"no US-* shard dirs under {args.shards_dir}")

    def failed_row(state: str) -> dict:
        return {
            "state": state,
            "records": 0,
            "new_persons": 0,
            "photos": 0,
            "errors": 1,
            "samples": [],
            "failed": True,
        }

    # Bind every state to its manifest before opening a database connection.
    # Missing, stale, extra, truncated, or digest-mismatched shards fail the
    # state instead of being silently globbed into (or out of) the load.
    overall: list[dict] = []
    state_zips: dict[str, list[Path]] = {}
    for state in states:
        try:
            if re.fullmatch(r"US-[A-Z]{2}", state) is None:
                raise ValueError("state must be a US-XX jurisdiction")
            if state in BLACKLIST:
                raise ValueError(f"blacklisted jurisdiction ({BLACKLIST[state]})")
            state_zips[state] = _state_zips(args.shards_dir, state)
        except Exception as exc:  # noqa: BLE001 - report every invalid state
            print(f"[{state}] invalid shard bundle: {exc}", flush=True)
            overall.append(failed_row(state))
    states = [state for state in states if state in state_zips]

    if not states:
        return 1

    from dotenv import load_dotenv
    load_dotenv()

    cfg = None
    if not args.dry_run:
        from web_scrubber.person.config import build_identity_service, load_config
        cfg = load_config(args.config, mode_override=args.to)
        print(f"target identity store: {cfg.mode}")

    if args.photos_only and not args.dry_run:
        print("=== PHOTO PHASE only (lock-free, no HBase): shard bytes -> HDFS ===", flush=True)
        run_photo_phase(
            states,
            args.shards_dir,
            cfg,
            force_unlock=args.force_unlock,
            limit=args.limit,
        )
        return 1 if overall else 0

    if args.bulk and not args.dry_run:
        # ONE bulk bundle: seed-once in-RAM index + batched writes + the global
        # lock (sole writer). On a connection drop the whole bundle is rebuilt
        # (re-seed from HBase) and the remaining + in-progress state are re-streamed
        # — idempotent, so already-flushed work is skipped by the cache.
        done: set[str] = set()
        for attempt in range(1, 26):
            remaining = [s for s in states if s not in done]
            if not remaining:
                break
            lead = ("BULK mode: " if attempt == 1
                    else f"BULK rebuild #{attempt} ({len(done)}/{len(states)} states done): ")
            print(f"{lead}single connection + global 'identity' lock + in-RAM index. "
                  f"Seeding from HBase ({len(remaining)} state(s) to stream)...", flush=True)
            bundle = build_identity_service(
                cfg, bulk=True, force_unlock=args.force_unlock,
                lock_owner="registry-faces:load-shards:bulk", lock_key="identity",
            )
            print("  seeded; streaming.", flush=True)
            try:
                for state in remaining:
                    zips = state_zips[state]
                    print(f"\n[{state}] {len(zips)} shard(s) [bulk]", flush=True)
                    row = bulk_load_one_state(bundle, state, zips, args.limit, args.refresh,
                                              meta_only=True)  # phase 1: records + pointers
                    bundle.store.flush()  # checkpoint this state durably
                    overall[:] = [r for r in overall if r["state"] != state]
                    overall.append(row)
                    done.add(state)
                    print(f"  -> records={row['records']} new_persons={row['new_persons']} "
                          f"photo_refs={row['photos']} errors={row['errors']}", flush=True)
                print("\nflushing final batches (phase 1: records + pointers loaded) ...", flush=True)
                _close_bundle(bundle)  # releases the global lock
                break
            except Exception as e:
                try:
                    _close_bundle(bundle, suppress_errors=True)
                except Exception:  # pragma: no cover - suppression is defensive
                    pass
                if isinstance(e, ConnectionLost) or _is_conn_error(e):
                    print(f"[bulk] connection dropped ({type(e).__name__}); rebuilding + "
                          f"resuming in 5s...", flush=True)
                    time.sleep(5)
                    continue
                raise
        else:
            print(f"[bulk] gave up after rebuilds; {len(done)}/{len(states)} states done", flush=True)
        unfinished = [state for state in states if state not in done]
        for state in unfinished:
            overall[:] = [row for row in overall if row["state"] != state]
            overall.append(failed_row(state))
        # PHASE 2: photo bytes -> HDFS, lock-free (the bundle's close() released the lock).
        if unfinished:
            print(f"[bulk] phase 1 incomplete ({len(done)}/{len(states)} states); "
                  f"skipping photo phase — rerun with --photos-only once records are in", flush=True)
        elif not args.records_only:
            print("\n=== PHASE 2: pushing photo bytes to HDFS (lock-free) ===", flush=True)
            run_photo_phase(
                states,
                args.shards_dir,
                cfg,
                force_unlock=args.force_unlock,
                limit=args.limit,
            )
    else:
        for state in states:
            zips = state_zips[state]
            print(f"\n[{state}] {len(zips)} shard(s){' (DRY RUN)' if args.dry_run else ''}"
                  f"{f' limit={args.limit}' if args.limit else ''}", flush=True)
            try:
                if args.dry_run:
                    row = dry_run_state(state, zips, args.limit)
                else:
                    row = load_state(cfg, state, zips, args, build_identity_service)
            except Exception:
                print(f"[{state}] FAILED — skipping to next state:\n"
                      f"{traceback.format_exc(limit=4)}", flush=True)
                overall.append(failed_row(state))
                continue
            overall.append(row)
            extra = f" recycles={row.get('recycles', 0)}"
            if row.get("reconnects"):
                extra += f" reconnects={row['reconnects']}"
            print(f"  -> records={row['records']} new_persons={row['new_persons']} "
                  f"photos={row['photos']} errors={row['errors']}{extra}")
            for jur, sid, name, nph in row["samples"]:
                print(f"     e.g. {jur}/{sid} {name!r} ({nph} photos)")

    print("\n" + "=" * 60)
    print(f"TOTAL: {sum(r['records'] for r in overall)} records, "
          f"{sum(r['photos'] for r in overall)} photos, "
          f"{sum(r['errors'] for r in overall)} errors, "
          f"{sum(r.get('reconnects', 0) for r in overall)} reconnects across {len(overall)} state(s)")
    failed = [r["state"] for r in overall if r.get("failed")]
    if failed:
        print(f"FAILED states ({len(failed)}): {', '.join(failed)}")
    return 1 if (failed or any(r["errors"] for r in overall)) else 0


if __name__ == "__main__":
    sys.exit(main())
