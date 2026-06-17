"""Map registry-faces records onto the centralized person identity.

Turns one ``adapter.run()`` item — ``(OffenderRecord, list[PhotoRef])`` — into the
``(Person, RegistryAttachment, list[person PhotoRef])`` triple the shared
IdentityService ingests. Registry sources give scalar height/weight, mapped to
``min==max`` ranges on the canonical Person.
"""

from __future__ import annotations

from web_scrubber.person.models import (
    Address,
    Offense,
    Person,
    PhotoRef,
    Registration,
    RegistryAttachment,
    Source,
)

from .schema import OffenderRecord


def _person(rec: OffenderRecord) -> Person:
    i = rec.identity
    return Person(
        full_name=i.full_name,
        aliases=list(i.aliases),
        dob=i.dob,
        year_of_birth=i.year_of_birth,
        sex=i.sex,
        race=i.race,
        height_cm_min=i.height_cm,
        height_cm_max=i.height_cm,
        weight_kg_min=i.weight_kg,
        weight_kg_max=i.weight_kg,
        eye_color=i.eye_color,
        hair_color=i.hair_color,
        description=i.description,
    )


def _attachment(rec: OffenderRecord) -> RegistryAttachment:
    s = rec.source
    return RegistryAttachment(
        source=Source(
            jurisdiction=s.jurisdiction,
            source_id=s.source_id,
            source_url=s.source_url,
            info_url=s.info_url,
            first_seen_at=s.first_seen_at,
            fetched_at=s.fetched_at,
        ),
        addresses=[Address(**a.model_dump()) for a in rec.addresses],
        offenses=[Offense(**o.model_dump()) for o in rec.offenses],
        registration=Registration(**rec.registration.model_dump()),
        raw=rec.raw,
    )


def _photos(rec: OffenderRecord, photos) -> list[PhotoRef]:
    s = rec.source
    out = []
    for ref in photos or []:
        out.append(
            PhotoRef(
                url=ref.url,
                source_jurisdiction=s.jurisdiction,
                source_id=s.source_id,
                domain="registry",
                source_type=getattr(ref, "source_type", "registry"),
            )
        )
    return out


def map_item(item) -> tuple[Person, RegistryAttachment, list[PhotoRef]]:
    """``(OffenderRecord, [PhotoRef])`` -> ``(Person, RegistryAttachment, [PhotoRef])``."""
    rec, photos = item
    return _person(rec), _attachment(rec), _photos(rec, photos)
