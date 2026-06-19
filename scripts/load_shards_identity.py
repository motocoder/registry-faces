"""Unpack downloaded shards and publish them into the centralized person identity.

Reads ``shards/US-XX/shard-NNN.zip`` (see ``download_shards.py``), reconstructs
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
import sys
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from registry_faces.identity_map import map_item
from registry_faces.schema import OffenderRecord


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
    with zipfile.ZipFile(zip_path) as zf:
        groups: dict[str, dict] = {}
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            parts = name.split("/")
            if len(parts) < 3:
                continue
            userdir = parts[1]
            g = groups.setdefault(userdir, {"record": None, "photos": []})
            if parts[-1] == "record.json" and len(parts) == 3:
                g["record"] = name
            elif len(parts) >= 4 and parts[2] == "photos":
                g["photos"].append(name)
        for userdir, g in groups.items():
            if not g["record"]:
                continue
            rec_bytes = zf.read(g["record"])
            photos = [(n.split("/")[-1], zf.read(n)) for n in sorted(g["photos"])]
            yield userdir, rec_bytes, photos


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
        if not meta_only:  # push the bytes now (non-two-phase path)
            key = bundle.blobs.put(data)
        if entry is None:
            entry = PhotoEntry(
                url=url, source_jurisdiction=jur, source_id=sid,
                domain="registry", source_type="registry",
            )
            pp.photos.append(entry)
            by_url[url] = entry
        entry.blob_key = key
        entry.sha256 = key
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


def iter_all_photo_bytes(zips: list[Path]):
    """Yield raw photo bytes across a state's shards (no record.json reads)."""
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                parts = name.split("/")
                if len(parts) >= 4 and parts[2] == "photos" and not name.endswith("/"):
                    yield zf.read(name)


def run_photo_phase(states: list[str], shards_dir: Path, cfg) -> int:
    """PHASE 2 (lock-free, NO HBase): push every shard photo's bytes into the HDFS
    blob store. The manifest pointers (blob_key = sha256) were written in phase 1;
    here we just land the bytes those pointers refer to. Content-addressed, so it
    dedups within the run and is safe to re-run. Returns photos pushed."""
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
            zips = sorted((shards_dir / state).glob("shard-*.zip"))
            if not zips:
                continue
            n = 0
            for data in iter_all_photo_bytes(zips):
                blobs.put(data)  # async push to HDFS; dedups by sha256
                n += 1
                mb += len(data) / 1024 / 1024
            total += n
            print(f"[{state}] photos queued: {n}  (cumulative {total}, {mb:.0f} MB)", flush=True)
    finally:
        print("waiting for final HDFS pushes ...", flush=True)
        blobs.close()
    print(f"PHOTO PHASE DONE: {total} photos, {mb:.0f} MB to HDFS", flush=True)
    return total


def dry_run_state(state: str, zips: list[Path], limit: int | None) -> dict:
    """--dry-run: reconstruct + map every record, no writes. Validates the data."""
    row = {"state": state, "records": 0, "new_persons": 0, "photos": 0,
           "errors": 0, "samples": [], "reconnects": 0, "recycles": 0}
    for userdir, rec_bytes, photos in iter_all_persons(zips):
        if limit and row["records"] >= limit:
            break
        try:
            rec = OffenderRecord.model_validate_json(rec_bytes)
            map_item((rec, []))
            row["records"] += 1
            _add_sample(row, rec.source.jurisdiction, rec.source.source_id,
                        rec.identity.full_name, len(photos))
        except Exception:
            row["errors"] += 1
            if row["errors"] <= 3:
                print(f"  ERROR {state}/{userdir}:\n{traceback.format_exc(limit=3)}", flush=True)
    return row


def _ingest_one(bundle, row: dict, rec_bytes: bytes, photos: list, refresh: bool,
                meta_only: bool = False) -> None:
    """Ingest one reconstructed record + its photo manifest. ``meta_only`` records
    photo pointers without pushing bytes (phase 1). Raises ConnectionLost on a
    transport drop (caller reopens + retries the same record); any other error is
    a per-record data error that the caller counts and skips."""
    rec = OffenderRecord.model_validate_json(rec_bytes)
    jur, sid = rec.source.jurisdiction, rec.source.source_id
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
                force_unlock=True,  # per-state lock is only ever ours; clear any stale hold
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


def _close_bundle(bundle) -> None:
    """Clean teardown: flush photo bytes to HDFS FIRST (so a dead Thrift socket
    can't skip it and orphan blob_key pointers in HBase), then close store/lock/conn."""
    if bundle is None:
        return
    try:
        bundle.blobs.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        bundle.close()
    except Exception:  # noqa: BLE001
        pass


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
            try:
                _ingest_one(bundle, row, rec_bytes, photos, args.refresh)
                pending = None
                stalls = 0
                row["records"] += 1
                since += 1
                last_ok = time.monotonic()
                if args.limit and row["records"] >= args.limit:
                    break
                # Optional proactive recycle (off by default; relies on a raised
                # server read-timeout to keep one connection alive instead).
                if (recycle_every > 0 and since >= recycle_every) or (
                    recycle_seconds > 0 and (time.monotonic() - last_recycle) >= recycle_seconds
                ):
                    _close_bundle(bundle)
                    bundle = _open_bundle(cfg, state, args, build_identity_service, max_stalls)
                    row["recycles"] += 1
                    since = 0
                    last_recycle = time.monotonic()
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
                _close_bundle(bundle)
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
        if args.verify:
            try:
                verify(bundle, state, row["samples"])
            except Exception:  # noqa: BLE001 — verify is best-effort
                pass
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
    for userdir, rec_bytes, photos in iter_all_persons(zips):
        if limit and row["records"] >= limit:
            break
        try:
            _ingest_one(bundle, row, rec_bytes, photos, refresh, meta_only=meta_only)
            row["records"] += 1
        except ConnectionLost:
            raise  # dead connection — let the caller rebuild + re-stream this state
        except Exception:
            row["errors"] += 1
            if row["errors"] <= 3:
                print(f"  ERROR {state}/{userdir}:\n{traceback.format_exc(limit=3)}", flush=True)
    return row


def verify(bundle, state: str, samples: list) -> None:
    """Read a few just-pushed persons back out of HBase and print the structure."""
    print(f"\n--- verify {state}: reading {len(samples)} persons back from the store ---")
    for jur, sid, name, nphotos in samples:
        uuid = bundle.store.lookup_source(jur, sid)
        if not uuid:
            print(f"  [{jur}/{sid}] NOT FOUND by source index — ingest did not index it!")
            continue
        person = bundle.store.get_person(uuid)
        atts = list(bundle.store.iter_attachments(uuid))
        pp = bundle.store.get_photos(uuid)
        photos = pp.photos if pp else []
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
        for e in photos[:2]:
            ok = "?"
            try:
                ok = "bytes-ok" if (e.blob_key and bundle.blobs.exists(e.blob_key)) else "MISSING"
            except Exception as ex:  # noqa: BLE001
                ok = f"check-failed({type(ex).__name__})"
            print(f"      - {e.url}  blob_key={(e.blob_key or '')[:12]}.. "
                  f"size={e.size_bytes} {ok}")


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

    if args.all:
        states = sorted(p.name for p in args.shards_dir.iterdir()
                        if p.is_dir() and p.name.startswith("US-"))
    elif args.state:
        states = [s.upper() for s in args.state]
    else:
        parser.error("pass --state US-XX (repeatable) or --all")
    if not states:
        parser.error(f"no US-* shard dirs under {args.shards_dir}")

    from dotenv import load_dotenv
    load_dotenv()

    cfg = None
    if not args.dry_run:
        from web_scrubber.person.config import build_identity_service, load_config
        cfg = load_config(args.config, mode_override=args.to)
        print(f"target identity store: {cfg.mode}")

    def _state_zips(state):
        return state, sorted((args.shards_dir / state).glob("shard-*.zip"))

    if args.photos_only and not args.dry_run:
        print("=== PHOTO PHASE only (lock-free, no HBase): shard bytes -> HDFS ===", flush=True)
        run_photo_phase(states, args.shards_dir, cfg)
        return 0

    overall = []
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
                cfg, bulk=True, force_unlock=True,
                lock_owner="registry-faces:load-shards:bulk", lock_key="identity",
            )
            print("  seeded; streaming.", flush=True)
            try:
                for state in remaining:
                    _, zips = _state_zips(state)
                    if not zips:
                        print(f"[{state}] no shard-*.zip — skipping")
                        done.add(state)
                        continue
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
                    _close_bundle(bundle)
                except Exception:  # noqa: BLE001 — teardown on a dead connection
                    pass
                if isinstance(e, ConnectionLost) or _is_conn_error(e):
                    print(f"[bulk] connection dropped ({type(e).__name__}); rebuilding + "
                          f"resuming in 5s...", flush=True)
                    time.sleep(5)
                    continue
                raise
        else:
            print(f"[bulk] gave up after rebuilds; {len(done)}/{len(states)} states done", flush=True)
        # PHASE 2: photo bytes -> HDFS, lock-free (the bundle's close() released the lock).
        if [s for s in states if s not in done]:
            print(f"[bulk] phase 1 incomplete ({len(done)}/{len(states)} states); "
                  f"skipping photo phase — rerun with --photos-only once records are in", flush=True)
        elif not args.records_only:
            print("\n=== PHASE 2: pushing photo bytes to HDFS (lock-free) ===", flush=True)
            run_photo_phase(states, args.shards_dir, cfg)
    else:
        for state in states:
            _, zips = _state_zips(state)
            if not zips:
                print(f"[{state}] no shard-*.zip under {args.shards_dir / state} — skipping")
                continue
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
                overall.append({"state": state, "records": 0, "new_persons": 0,
                                "photos": 0, "errors": 1, "samples": [], "failed": True})
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
