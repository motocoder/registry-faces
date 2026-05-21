"""Filesystem-backed store.

Layout:

    <registry root>/
    ├── records/
    │   ├── US-HI/
    │   │   ├── 12345/
    │   │   │   ├── record.json
    │   │   │   └── photos/
    │   │   │       ├── manifest.json
    │   │   │       ├── 001-registry.jpg
    │   │   │       └── ...
    │   ├── US-FL/
    │   │   └── ...
    ├── indexes/
    │   ├── index.jsonl       one line per record, used for search
    │   └── manifest.json     per-jurisdiction counts + last-ingest timestamps

Idempotent merge: re-ingesting the same source merges new data into existing
records. Null values never overwrite non-null values. Lists union by natural
key. People who disappear from the source feed keep their folder.

The in-memory index is loaded on open and flushed on close. Use as a context
manager (`with FileStore(root) as s:`) to ensure flush happens.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .photos import PhotoRef, merge_photo_refs
from .schema import Address, Identity, Offense, OffenderRecord, Registration, Source


# ---------------------------------------------------------------------------
# Filename safety


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize(name: str) -> str:
    return _SAFE_RE.sub("_", name) or "_"


# ---------------------------------------------------------------------------
# Merge primitives


def _addr_key(a: Address) -> tuple:
    return (
        a.type,
        (a.street or "").strip().lower(),
        (a.city or "").strip().lower(),
        (a.state or "").strip().lower(),
        (a.zip or "").strip().lower(),
    )


def _offense_key(o: Offense) -> tuple:
    return (
        (o.raw_code or "").strip().lower(),
        (o.raw_description or "").strip().lower(),
        o.conviction_date.isoformat() if o.conviction_date else "",
    )


def _merge_addresses(old: list[Address], new: list[Address]) -> list[Address]:
    by_key: dict[tuple, Address] = {_addr_key(a): a for a in old}
    for a in new:
        k = _addr_key(a)
        if k in by_key:
            existing = by_key[k]
            # Refresh verified_at if newer
            if a.verified_at and (
                existing.verified_at is None or a.verified_at > existing.verified_at
            ):
                existing.verified_at = a.verified_at
            # Fill in any missing geo info
            if existing.lat is None and a.lat is not None:
                existing.lat = a.lat
            if existing.lon is None and a.lon is not None:
                existing.lon = a.lon
        else:
            by_key[k] = a
    return list(by_key.values())


def _merge_offenses(old: list[Offense], new: list[Offense]) -> list[Offense]:
    by_key: dict[tuple, Offense] = {_offense_key(o): o for o in old}
    for o in new:
        k = _offense_key(o)
        if k not in by_key:
            by_key[k] = o
    return list(by_key.values())


def _merge_aliases(old: list[str], new: list[str]) -> list[str]:
    seen: dict[str, str] = {}  # lowercased -> original
    for a in old + new:
        k = a.strip().lower()
        if k and k not in seen:
            seen[k] = a.strip()
    return list(seen.values())


def _merge_identity(old: Identity, new: Identity) -> Identity:
    """Scalar fields: incoming wins if non-null. Aliases: union."""
    merged_data = old.model_dump()
    for field, value in new.model_dump().items():
        if field == "aliases":
            continue
        if value not in (None, "", "unknown"):
            merged_data[field] = value
    merged_data["aliases"] = _merge_aliases(old.aliases, new.aliases)
    return Identity(**merged_data)


def _merge_source(old: Source, new: Source) -> Source:
    return Source(
        jurisdiction=old.jurisdiction,
        source_id=old.source_id,
        source_url=new.source_url or old.source_url,
        info_url=new.info_url or old.info_url,
        first_seen_at=old.first_seen_at or new.first_seen_at or new.fetched_at,
        fetched_at=new.fetched_at,
    )


def merge_records(existing: OffenderRecord, incoming: OffenderRecord) -> OffenderRecord:
    """Apply the merge rules described at the top of this module."""
    return OffenderRecord(
        source=_merge_source(existing.source, incoming.source),
        identity=_merge_identity(existing.identity, incoming.identity),
        addresses=_merge_addresses(existing.addresses, incoming.addresses),
        offenses=_merge_offenses(existing.offenses, incoming.offenses),
        registration=incoming.registration,  # status latest wins
        raw=incoming.raw if incoming.raw is not None else existing.raw,
    )


# ---------------------------------------------------------------------------
# Index entry


def _build_index_entry(record: OffenderRecord, person_dir_rel: str) -> dict:
    addrs = [
        {
            "city": a.city,
            "state": a.state,
            "zip": a.zip,
            "lat": a.lat,
            "lon": a.lon,
        }
        for a in record.addresses
    ]
    return {
        "jurisdiction": record.source.jurisdiction,
        "source_id": record.source.source_id,
        "full_name": record.identity.full_name,
        "addresses": addrs,
        "path": person_dir_rel,
    }


# ---------------------------------------------------------------------------
# FileStore


class FileStore:
    def __init__(self, root: str | Path = "registry") -> None:
        self.root = Path(root)
        self.records_dir = self.root / "records"
        self.indexes_dir = self.root / "indexes"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.indexes_dir.mkdir(parents=True, exist_ok=True)

        # In-memory index: (jurisdiction, source_id) -> entry dict
        self._index: dict[tuple[str, str], dict] = {}
        self._index_dirty = False
        self._load_index()

    # ---- context manager ------------------------------------------------

    def __enter__(self) -> "FileStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._index_dirty:
            self._flush_index()
            self._write_manifest()
            self._index_dirty = False

    # ---- paths ----------------------------------------------------------

    def person_dir(self, jurisdiction: str, source_id: str) -> Path:
        return self.records_dir / _sanitize(jurisdiction) / _sanitize(source_id)

    def _person_dir_rel(self, jurisdiction: str, source_id: str) -> str:
        return str(self.person_dir(jurisdiction, source_id).relative_to(self.root))

    # ---- upsert ---------------------------------------------------------

    def upsert(
        self,
        record: OffenderRecord,
        photos: list[PhotoRef] | None = None,
    ) -> OffenderRecord:
        """Merge `record` into the store. Returns the final merged record."""
        photos = photos or []
        person_dir = self.person_dir(record.source.jurisdiction, record.source.source_id)
        person_dir.mkdir(parents=True, exist_ok=True)
        record_path = person_dir / "record.json"

        if record_path.exists():
            existing = OffenderRecord.model_validate_json(record_path.read_text())
            merged = merge_records(existing, record)
        else:
            # First sighting — ensure first_seen_at is set
            if record.source.first_seen_at is None:
                record.source.first_seen_at = record.source.fetched_at
            merged = record

        record_path.write_text(merged.model_dump_json(indent=2) + "\n")

        if photos:
            merge_photo_refs(
                person_dir,
                person_key={
                    "jurisdiction": merged.source.jurisdiction,
                    "source_id": merged.source.source_id,
                },
                refs=photos,
            )

        # Update in-memory index
        entry = _build_index_entry(merged, self._person_dir_rel(merged.source.jurisdiction, merged.source.source_id))
        self._index[(merged.source.jurisdiction, merged.source.source_id)] = entry
        self._index_dirty = True

        return merged

    def upsert_many(self, records: Iterable[OffenderRecord]) -> int:
        count = 0
        for r in records:
            self.upsert(r)
            count += 1
        return count

    # ---- read -----------------------------------------------------------

    def get(self, jurisdiction: str, source_id: str) -> OffenderRecord | None:
        path = self.person_dir(jurisdiction, source_id) / "record.json"
        if not path.exists():
            return None
        return OffenderRecord.model_validate_json(path.read_text())

    def count(self) -> int:
        return len(self._index)

    def search_name(self, query: str, limit: int = 50) -> list[OffenderRecord]:
        q = query.lower()
        out: list[OffenderRecord] = []
        for entry in self._index.values():
            if q in entry["full_name"].lower():
                rec = self.get(entry["jurisdiction"], entry["source_id"])
                if rec is not None:
                    out.append(rec)
                if len(out) >= limit:
                    break
        return out

    def search_zip(self, zip_code: str) -> list[OffenderRecord]:
        out: list[OffenderRecord] = []
        for entry in self._index.values():
            for addr in entry["addresses"]:
                if addr.get("zip") == zip_code:
                    rec = self.get(entry["jurisdiction"], entry["source_id"])
                    if rec is not None:
                        out.append(rec)
                    break
        return out

    def search_radius(self, lat: float, lon: float, radius_meters: float) -> list[OffenderRecord]:
        lat_delta = radius_meters / 111_111
        cos_lat = max(0.0001, abs(math.cos(math.radians(lat))))
        lon_delta = radius_meters / (111_111 * cos_lat)
        lat_lo, lat_hi = lat - lat_delta, lat + lat_delta
        lon_lo, lon_hi = lon - lon_delta, lon + lon_delta

        out: list[OffenderRecord] = []
        for entry in self._index.values():
            for addr in entry["addresses"]:
                a_lat, a_lon = addr.get("lat"), addr.get("lon")
                if a_lat is None or a_lon is None:
                    continue
                if lat_lo <= a_lat <= lat_hi and lon_lo <= a_lon <= lon_hi:
                    rec = self.get(entry["jurisdiction"], entry["source_id"])
                    if rec is not None:
                        out.append(rec)
                    break
        return out

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._index.values():
            counts[entry["jurisdiction"]] = counts.get(entry["jurisdiction"], 0) + 1
        return counts

    # ---- index ----------------------------------------------------------

    def _load_index(self) -> None:
        path = self.indexes_dir / "index.jsonl"
        if not path.exists():
            return
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                self._index[(entry["jurisdiction"], entry["source_id"])] = entry

    def _flush_index(self) -> None:
        path = self.indexes_dir / "index.jsonl"
        sorted_keys = sorted(self._index.keys())
        with path.open("w") as f:
            for key in sorted_keys:
                f.write(json.dumps(self._index[key], separators=(",", ":")) + "\n")

    def _write_manifest(self) -> None:
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_records": self.count(),
            "by_jurisdiction": self.stats(),
        }
        (self.indexes_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )

    def rebuild_index(self) -> int:
        """Walk records/ and regenerate the in-memory index. Returns record count."""
        self._index.clear()
        for jur_dir in sorted(self.records_dir.iterdir()):
            if not jur_dir.is_dir():
                continue
            for person_dir in sorted(jur_dir.iterdir()):
                if not person_dir.is_dir():
                    continue
                rec_path = person_dir / "record.json"
                if not rec_path.exists():
                    continue
                rec = OffenderRecord.model_validate_json(rec_path.read_text())
                rel = str(person_dir.relative_to(self.root))
                self._index[(rec.source.jurisdiction, rec.source.source_id)] = (
                    _build_index_entry(rec, rel)
                )
        self._index_dirty = True
        return len(self._index)


# Backwards-compat alias
Store = FileStore
