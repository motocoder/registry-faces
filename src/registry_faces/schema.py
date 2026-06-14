"""Canonical schema for offender records, normalized across jurisdictions.

The shape is intentionally permissive: most fields are optional because different
registries publish different subsets. The only required fields are the source
key triple and `identity.full_name`. Everything else may be absent.

Photo metadata lives in `photos/manifest.json` next to each record, not in the
record itself — see `registry_faces.photos`.

Always preserve the original payload in `raw` so we can re-derive normalized
fields later without re-fetching.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _new_guid() -> str:
    return str(uuid.uuid4())

AddressType = Literal["home", "work", "school", "temporary", "other"]
Sex = Literal["M", "F", "X", "unknown"]
RegistrationStatus = Literal[
    "active", "absconder", "incarcerated", "deceased", "removed", "unknown"
]


class Address(BaseModel):
    type: AddressType = "home"
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str = "US"
    lat: float | None = None
    lon: float | None = None
    verified_at: datetime | None = None


class Offense(BaseModel):
    raw_code: str | None = None
    raw_description: str
    normalized_category: str | None = None
    conviction_date: datetime | None = None
    jurisdiction: str | None = None
    statute: str | None = None
    tier_or_level_raw: str | None = None


class Identity(BaseModel):
    # Stable per-person UUID. Generated when an Identity is first created and
    # preserved across re-ingests by `_merge_identity`. Not derived from any
    # source field, so it survives renames and source_id changes.
    guid: str = Field(default_factory=_new_guid)
    full_name: str
    aliases: list[str] = Field(default_factory=list)
    dob: datetime | None = None
    year_of_birth: int | None = None
    sex: Sex = "unknown"
    race: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    eye_color: str | None = None
    hair_color: str | None = None
    description: str | None = None


class Source(BaseModel):
    jurisdiction: str
    source_id: str
    source_url: str | None = None
    info_url: str | None = None
    first_seen_at: datetime | None = None
    fetched_at: datetime


class Registration(BaseModel):
    status: RegistrationStatus = "unknown"
    registered_since: datetime | None = None
    next_verification: datetime | None = None
    absconder: bool = False


class OffenderRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: Source
    identity: Identity
    addresses: list[Address] = Field(default_factory=list)
    offenses: list[Offense] = Field(default_factory=list)
    registration: Registration = Field(default_factory=Registration)
    raw: dict | None = None
