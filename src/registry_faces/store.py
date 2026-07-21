"""Filesystem store — registry-faces binding over ``web_scrubber.store``.

The generic ``FileStore`` (index, paths, manifest, merge engine) lives in the
framework. This module keeps the **domain** part: how two ``OffenderRecord``
records merge, the ``StoreSpec`` wiring, and the registry-specific read methods
(geo search by zip/radius, and the legacy ``backfill_guids`` migration).

Layout on disk is unchanged: ``records/<jurisdiction>/<source_id>/``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from web_scrubber import merge as _merge
from web_scrubber.store import FileStore as _BaseStore
from web_scrubber.store import StoreSpec

from .schema import Address, Identity, Offense, OffenderRecord, Source


_EARTH_RADIUS_METERS = 6_371_008.8


# ---------------------------------------------------------------------------
# Merge key functions (domain-specific)


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
    return _merge.merge_addresses(old, new, _addr_key, newer_attr="verified_at")


def _merge_identity(old: Identity, new: Identity) -> Identity:
    """Scalar fields: incoming wins if non-null. Aliases: union. ``guid`` stable."""
    merged_data = _merge.merge_scalars(
        old.model_dump(), new.model_dump(), skip={"aliases", "guid"}
    )
    merged_data["aliases"] = _merge.merge_aliases(old.aliases, new.aliases)
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
    """Idempotent merge: null never overwrites, lists union, registration latest-wins."""
    return OffenderRecord(
        source=_merge_source(existing.source, incoming.source),
        identity=_merge_identity(existing.identity, incoming.identity),
        addresses=_merge_addresses(existing.addresses, incoming.addresses),
        offenses=_merge.union_by_key(existing.offenses, incoming.offenses, _offense_key),
        registration=incoming.registration,
        raw=incoming.raw if incoming.raw is not None else existing.raw,
    )


# ---------------------------------------------------------------------------
# StoreSpec wiring


def _key_of(rec: OffenderRecord) -> tuple[str, str]:
    return (rec.source.jurisdiction, rec.source.source_id)


def _set_first_seen(rec: OffenderRecord) -> None:
    if rec.source.first_seen_at is None:
        rec.source.first_seen_at = rec.source.fetched_at


def _index_entry(rec: OffenderRecord, rel: str) -> dict:
    addrs = [
        {"city": a.city, "state": a.state, "zip": a.zip, "lat": a.lat, "lon": a.lon}
        for a in rec.addresses
    ]
    return {
        "jurisdiction": rec.source.jurisdiction,
        "source_id": rec.source.source_id,
        "path": rel,
        "name": rec.identity.full_name,
        "full_name": rec.identity.full_name,
        "guid": rec.identity.guid,
        "addresses": addrs,
    }


SPEC: StoreSpec = StoreSpec(
    record_cls=OffenderRecord,
    key_of=_key_of,
    merge=merge_records,
    index_entry_of=_index_entry,
    on_first_seen=_set_first_seen,
)


class FileStore(_BaseStore):
    """Source-keyed store bound to the registry-faces schema.

    Adds the registry-specific geo search and the legacy guid backfill on top
    of the generic store (name search / get / get_by_guid / stats /
    rebuild_index are inherited).
    """

    def __init__(self, root="registry") -> None:
        super().__init__(root, SPEC)

    # ---- geo search (domain) -------------------------------------------

    def search_zip(self, zip_code: str) -> list:
        out = []
        for entry in self._index.values():
            for addr in entry.get("addresses", []):
                if addr.get("zip") == zip_code:
                    rec = self.get(entry["jurisdiction"], entry["source_id"])
                    if rec is not None:
                        out.append(rec)
                    break
        return out

    def search_radius(self, lat: float, lon: float, radius_meters: float) -> list:
        if not -90.0 <= lat <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if radius_meters < 0:
            raise ValueError("radius_meters cannot be negative")

        # Use a cheap spherical bounding box only as a prefilter.  The old
        # implementation returned the whole box, including points well outside
        # the requested circle, and failed around the antimeridian.  Longitude
        # is compared as a wrapped delta so +179/-179 remain neighbors.
        angular = radius_meters / _EARTH_RADIUS_METERS
        lat_rad = math.radians(lat)
        lat_delta = math.degrees(angular)
        lat_lo = max(-90.0, lat - lat_delta)
        lat_hi = min(90.0, lat + lat_delta)
        if angular >= (math.pi / 2) - abs(lat_rad):
            lon_delta = 180.0
        else:
            ratio = math.sin(angular) / max(abs(math.cos(lat_rad)), 1e-15)
            lon_delta = math.degrees(math.asin(min(1.0, abs(ratio))))

        out = []
        for entry in self._index.values():
            for addr in entry.get("addresses", []):
                a_lat, a_lon = addr.get("lat"), addr.get("lon")
                if a_lat is None or a_lon is None:
                    continue
                wrapped_lon_delta = abs(((a_lon - lon + 180.0) % 360.0) - 180.0)
                if (
                    lat_lo <= a_lat <= lat_hi
                    and wrapped_lon_delta <= lon_delta
                    and _haversine_meters(lat, lon, a_lat, a_lon) <= radius_meters
                ):
                    rec = self.get(entry["jurisdiction"], entry["source_id"])
                    if rec is not None:
                        out.append(rec)
                    break
        return out

    # ---- legacy guid backfill (domain migration) -----------------------

    def backfill_guids(self) -> int:
        """Persist a stable guid on any record written before guid existed.

        Reads the raw JSON, generates a guid where the field is absent, writes
        it back, and refreshes the in-memory index. Returns the count updated.
        """
        updated = 0
        for jur_dir in sorted(self.records_dir.iterdir()):
            if not jur_dir.is_dir():
                continue
            for person_dir in sorted(jur_dir.iterdir()):
                if not person_dir.is_dir():
                    continue
                rec_path = person_dir / "record.json"
                if not rec_path.exists():
                    continue
                raw = json.loads(rec_path.read_text(encoding="utf-8"))
                identity = raw.get("identity") or {}
                if identity.get("guid"):
                    continue
                rec = OffenderRecord.model_validate(raw)  # auto-fills guid
                rec_path.write_text(rec.model_dump_json(indent=2) + "\n", encoding="utf-8")
                rel = str(person_dir.relative_to(self.root))
                self._index[_key_of(rec)] = _index_entry(rec, rel)
                self._index_dirty = True
                updated += 1
        return updated


# Backwards-compat alias
Store = FileStore


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance with a wrapped longitude delta."""

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(((lon2 - lon1 + 180.0) % 360.0) - 180.0)
    hav = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1.0, hav)))
