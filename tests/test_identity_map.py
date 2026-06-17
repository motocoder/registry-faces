"""registry-faces -> centralized person identity mapping + ingest (file/dry-run)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from web_scrubber.person.config import IdentityConfig, build_identity_service
from web_scrubber.person.ingest import ingest_adapter

from registry_faces.identity_map import map_item
from registry_faces.photos import PhotoRef
from registry_faces.schema import Address, Identity, Offense, OffenderRecord, Registration, Source


def _record() -> OffenderRecord:
    return OffenderRecord(
        source=Source(jurisdiction="US-HI", source_id="HI1", fetched_at=datetime.now(timezone.utc)),
        identity=Identity(full_name="Jane Roe", sex="F", height_cm=165.0),
        addresses=[Address(type="home", state="HI", zip="96813")],
        offenses=[Offense(raw_description="Demo offense")],
        registration=Registration(status="active"),
    )


class _Adapter:
    def run(self):
        yield (_record(), [PhotoRef(url="https://x/p.jpg", source_type="registry")])


def test_map_and_ingest_file_mode(tmp_path: Path):
    cfg = IdentityConfig(mode="file", file_root=tmp_path / "id")
    with build_identity_service(cfg) as b:
        stats = ingest_adapter(b.service, _Adapter(), map_item)
        assert stats.records == 1 and stats.new_persons == 1 and stats.photos_added == 1

        uuid = next(iter(b.store.iter_person_uuids()))
        bundle = b.service.get_bundle(uuid)
        att = bundle.attachments[0]
        assert att.domain == "registry"
        assert att.registration.status == "active"
        assert att.offenses[0].raw_description == "Demo offense"
        # scalar height -> min==max range on the canonical person
        assert bundle.person.height_cm_min == 165.0 and bundle.person.height_cm_max == 165.0
