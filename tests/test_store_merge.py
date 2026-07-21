"""Merge invariants for FileStore — the contract users depend on across re-ingests.

These are the rules:
  * Null / "unknown" never overwrites a non-null value on identity scalars.
  * identity.guid is preserved across re-ingests (incoming guid is ignored).
  * Aliases / addresses / offenses union without duplicating.
  * Addresses dedup by (type, street, city, state, zip) lowercased; existing
    entries get `verified_at` refreshed and missing `lat`/`lon` filled in.
  * Source.first_seen_at is set on first ingest and never changes.
  * Source.fetched_at always updates to the latest ingest.
  * Registration latest-wins (it's a status, supposed to change).
  * raw always replaced with the latest payload.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from registry_faces.schema import (
    Address,
    Identity,
    Offense,
    OffenderRecord,
    Registration,
    Source,
)
from registry_faces.store import FileStore, merge_records


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _record(
    *,
    source_id: str = "1",
    full_name: str = "Alice Example",
    aliases: list[str] | None = None,
    sex: str = "F",
    addresses: list[Address] | None = None,
    offenses: list[Offense] | None = None,
    registration: Registration | None = None,
    raw: dict | None = None,
    fetched_at: datetime = T0,
    first_seen_at: datetime | None = None,
) -> OffenderRecord:
    return OffenderRecord(
        source=Source(
            jurisdiction="US-XX",
            source_id=source_id,
            fetched_at=fetched_at,
            first_seen_at=first_seen_at,
        ),
        identity=Identity(full_name=full_name, aliases=aliases or [], sex=sex),
        addresses=addresses or [],
        offenses=offenses or [],
        registration=registration or Registration(),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# merge_records — pure function tests


def test_first_seen_at_preserved_across_reingest():
    a = _record(fetched_at=T0, first_seen_at=T0)
    b = _record(fetched_at=T1, first_seen_at=T1)  # adapter sets it to "now" again
    merged = merge_records(a, b)
    assert merged.source.first_seen_at == T0
    assert merged.source.fetched_at == T1


def test_null_does_not_overwrite_value():
    a = _record(sex="F")
    b = _record(sex="unknown")
    assert merge_records(a, b).identity.sex == "F"


def test_non_null_overwrites_old_value():
    a = _record(sex="unknown")
    b = _record(sex="F")
    assert merge_records(a, b).identity.sex == "F"


def test_aliases_union_case_insensitive():
    a = _record(aliases=["AE", "Other Name"])
    b = _record(aliases=["ae", "Third Name"])  # different case of first
    merged = merge_records(a, b)
    lowered = {x.lower() for x in merged.identity.aliases}
    assert lowered == {"ae", "other name", "third name"}


def test_addresses_dedup_by_natural_key():
    addr = Address(type="home", street="1 Main", city="Pierre", state="SD", zip="57501")
    a = _record(addresses=[addr])
    b = _record(addresses=[addr.model_copy()])
    assert len(merge_records(a, b).addresses) == 1


def test_addresses_lat_lon_backfilled():
    bare = Address(type="home", street="1 Main", city="Pierre", state="SD", zip="57501")
    with_geo = Address(
        type="home", street="1 Main", city="Pierre", state="SD", zip="57501",
        lat=44.3, lon=-100.3,
    )
    merged = merge_records(_record(addresses=[bare]), _record(addresses=[with_geo]))
    assert len(merged.addresses) == 1
    assert merged.addresses[0].lat == 44.3
    assert merged.addresses[0].lon == -100.3


def test_addresses_append_new():
    home = Address(type="home", street="1 Main", city="Pierre", state="SD", zip="57501")
    work = Address(type="work", street="2 Job Rd", city="Pierre", state="SD", zip="57501")
    merged = merge_records(_record(addresses=[home]), _record(addresses=[work]))
    assert len(merged.addresses) == 2


def test_offenses_dedup():
    o = Offense(raw_code="X", raw_description="Demo")
    merged = merge_records(_record(offenses=[o]), _record(offenses=[o.model_copy()]))
    assert len(merged.offenses) == 1


def test_offenses_append_new():
    a = Offense(raw_code="X", raw_description="First")
    b = Offense(raw_code="Y", raw_description="Second")
    merged = merge_records(_record(offenses=[a]), _record(offenses=[b]))
    assert len(merged.offenses) == 2


def test_registration_latest_wins():
    a = _record(registration=Registration(status="active"))
    b = _record(registration=Registration(status="absconder", absconder=True))
    merged = merge_records(a, b)
    assert merged.registration.status == "absconder"
    assert merged.registration.absconder is True


def test_raw_replaced_with_latest():
    a = _record(raw={"v": 1})
    b = _record(raw={"v": 2})
    assert merge_records(a, b).raw == {"v": 2}


def test_raw_preserved_when_incoming_is_none():
    a = _record(raw={"v": 1})
    b = _record(raw=None)
    assert merge_records(a, b).raw == {"v": 1}


# ---------------------------------------------------------------------------
# FileStore — round-trip + idempotency


def test_filestore_first_ingest_sets_first_seen_at(tmp_path: Path):
    with FileStore(tmp_path) as s:
        merged = s.upsert(_record(fetched_at=T0))
    assert merged.source.first_seen_at == T0
    assert merged.source.fetched_at == T0


def test_filestore_reingest_is_idempotent(tmp_path: Path):
    rec1 = _record(
        addresses=[Address(type="home", street="1 Main", city="Pierre", state="SD", zip="57501")],
        fetched_at=T0,
    )
    rec2 = _record(
        addresses=[
            Address(type="home", street="1 Main", city="Pierre", state="SD", zip="57501"),
            Address(type="work", street="2 Job Rd", city="Pierre", state="SD", zip="57501"),
        ],
        fetched_at=T1,
    )
    with FileStore(tmp_path) as s:
        s.upsert(rec1)
    with FileStore(tmp_path) as s:
        s.upsert(rec2)
        out = s.get("US-XX", "1")
    assert out is not None
    assert out.source.first_seen_at == T0
    assert out.source.fetched_at == T1
    assert len(out.addresses) == 2


def test_filestore_search_by_name(tmp_path: Path):
    with FileStore(tmp_path) as s:
        s.upsert(_record(source_id="1", full_name="Alice Anderson"))
        s.upsert(_record(source_id="2", full_name="Bob Brown"))
    with FileStore(tmp_path) as s:
        hits = s.search_name("ander")
    assert len(hits) == 1
    assert hits[0].identity.full_name == "Alice Anderson"


def test_filestore_search_by_radius(tmp_path: Path):
    addr_in = Address(type="home", lat=44.3, lon=-100.3)
    addr_out = Address(type="home", lat=40.0, lon=-90.0)
    with FileStore(tmp_path) as s:
        s.upsert(_record(source_id="in", addresses=[addr_in]))
        s.upsert(_record(source_id="out", addresses=[addr_out]))
    with FileStore(tmp_path) as s:
        hits = s.search_radius(44.3, -100.3, 5000)
    assert {r.source.source_id for r in hits} == {"in"}


def test_filestore_search_radius_filters_bounding_box_corners(tmp_path: Path):
    # Both coordinate deltas fit inside the old 1 km square, but their
    # diagonal great-circle distance is roughly 1.26 km.
    corner = Address(type="home", lat=0.008, lon=0.008)
    inside = Address(type="home", lat=0.004, lon=0.004)
    with FileStore(tmp_path) as store:
        store.upsert(_record(source_id="corner", addresses=[corner]))
        store.upsert(_record(source_id="inside", addresses=[inside]))
    with FileStore(tmp_path) as store:
        hits = store.search_radius(0.0, 0.0, 1000.0)
    assert {record.source.source_id for record in hits} == {"inside"}


def test_filestore_search_radius_wraps_antimeridian(tmp_path: Path):
    across_dateline = Address(type="home", lat=0.0, lon=-179.999)
    with FileStore(tmp_path) as store:
        store.upsert(_record(source_id="across", addresses=[across_dateline]))
    with FileStore(tmp_path) as store:
        hits = store.search_radius(0.0, 179.999, 500.0)
    assert {record.source.source_id for record in hits} == {"across"}


def test_identity_guid_autogenerated_and_unique():
    a = Identity(full_name="A")
    b = Identity(full_name="B")
    assert a.guid and b.guid
    assert a.guid != b.guid


def test_merge_preserves_existing_guid():
    a = _record()
    b = _record()
    assert a.identity.guid != b.identity.guid  # fresh records, distinct guids
    merged = merge_records(a, b)
    assert merged.identity.guid == a.identity.guid


def test_filestore_guid_stable_across_reingest(tmp_path: Path):
    with FileStore(tmp_path) as s:
        r1 = s.upsert(_record(fetched_at=T0))
    with FileStore(tmp_path) as s:
        r2 = s.upsert(_record(fetched_at=T1))
    assert r1.identity.guid == r2.identity.guid


def test_filestore_get_by_guid(tmp_path: Path):
    with FileStore(tmp_path) as s:
        merged = s.upsert(_record(source_id="1", full_name="Alice"))
        guid = merged.identity.guid
    with FileStore(tmp_path) as s:
        found = s.get_by_guid(guid)
    assert found is not None
    assert found.identity.full_name == "Alice"


def test_backfill_guids_assigns_to_records_missing_one(tmp_path: Path):
    # Hand-write a record.json with no `guid` field — simulates a record
    # written before the schema gained guid.
    legacy_dir = tmp_path / "records" / "US-XX" / "legacy"
    legacy_dir.mkdir(parents=True)
    legacy_payload = {
        "source": {
            "jurisdiction": "US-XX",
            "source_id": "legacy",
            "fetched_at": T0.isoformat(),
            "first_seen_at": T0.isoformat(),
        },
        "identity": {"full_name": "Legacy Person", "aliases": [], "sex": "unknown"},
        "addresses": [],
        "offenses": [],
        "registration": {"status": "unknown", "absconder": False},
        "raw": None,
    }
    (legacy_dir / "record.json").write_text(json.dumps(legacy_payload))

    with FileStore(tmp_path) as s:
        updated = s.backfill_guids()
    assert updated == 1

    on_disk = json.loads((legacy_dir / "record.json").read_text())
    assert on_disk["identity"]["guid"]

    # Idempotent — no further work on a second pass.
    with FileStore(tmp_path) as s:
        assert s.backfill_guids() == 0


def test_filestore_rebuild_index(tmp_path: Path):
    with FileStore(tmp_path) as s:
        s.upsert(_record(source_id="1"))
        s.upsert(_record(source_id="2"))
    # Delete the index file to simulate corruption
    (tmp_path / "indexes" / "index.jsonl").unlink()
    with FileStore(tmp_path) as s:
        n = s.rebuild_index()
    assert n == 2
