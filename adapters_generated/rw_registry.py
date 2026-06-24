"""Rwanda — National Public Prosecution Authority (NPPA) Sex Offenders Registry.

Source type: paginated JSON API (Spring `Pageable`) behind an Angular SPA.

Rwanda is one of the rare non-US jurisdictions that runs an OFFICIAL, public,
enumerable sex-offender registry. The NPPA ("Ubushinjacyaha Bukuru") publishes
convicted rape/defilement offenders at:

    https://sor.nppa.gov.rw/

The SPA reads a public, unauthenticated REST endpoint that returns a standard
paged envelope (`content` + `totalPages`/`last`):

    GET https://sor.nppa.gov.rw/pub/publishedOffenders?expand=victims&page=N&size=K

Each record carries name, parent names, gender (Kinyarwanda GABO/GORE), DOB
(with a precision flag), place codes, court case number, occupation, an offence
narrative, the sentence, and victim sub-records.

Photos: the API inlines each portrait as raw base64 in the `picture` field —
there is NO separate image URL the source serves. Per scope, `extract_photos`
only returns source-published image *URLs*; with none available it returns an
empty list. The base64 stays untouched in `record.raw` for a later step.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from registry_faces.schema import (
    Address,
    Identity,
    Offense,
    OffenderRecord,
    Registration,
    Source,
)
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://sor.nppa.gov.rw"
API = f"{BASE}/pub"
LISTING = f"{API}/publishedOffenders"
USER_AGENT = "registry-faces/1.0 (+https://sor.nppa.gov.rw public NPPA SOR index)"

PAGE_SIZE = 100
# Safety bound: ~1.9k records at 100/page is ~19 pages. Stop on `last`/empty;
# this cap only prevents a runaway loop if the envelope shape changes.
MAX_PAGES = 200

# Kinyarwanda gender tokens used by the registry.
_SEX = {"GABO": "M", "GORE": "F"}

_STATUS = {
    "ACTIVE": "active",
    "REMOVED": "removed",
    "DECEASED": "deceased",
}


def _gender(value: str | None) -> str:
    return _SEX.get((value or "").strip().upper(), "unknown")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class RwRegistryAdapter(Adapter):
    jurisdiction = "RW"
    source_name = "Rwanda NPPA Sex Offenders Registry"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=60.0,
            follow_redirects=True,
        )

    # -- fetch ----------------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        for page in range(MAX_PAGES):
            resp = self._client.get(
                LISTING,
                params={"expand": "victims", "page": page, "size": PAGE_SIZE},
            )
            resp.raise_for_status()
            body = resp.json()
            content = body.get("content") or []
            if not content:
                break
            yield from content
            if body.get("last") is True:
                break

    # -- normalize ------------------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)

        # DOB: honour the source precision flag — only treat it as a full
        # birthdate when the source says so; otherwise keep just the year.
        dob = None
        year_of_birth = None
        parsed_dob = _parse_date(raw.get("dateOfBirth"))
        if parsed_dob is not None:
            if (raw.get("dateOfBirthPrecision") or "").upper() == "FULL_DATE":
                dob = parsed_dob
            year_of_birth = parsed_dob.year

        occupation = (raw.get("occupation") or "").strip() or None
        identity = Identity(
            full_name=(raw.get("name") or "").strip(),
            sex=_gender(raw.get("gender")),
            dob=dob,
            year_of_birth=year_of_birth,
            description=occupation,
        )

        offenses: list[Offense] = []
        offence_text = (raw.get("offence") or "").strip()
        if offence_text:
            offenses.append(
                Offense(
                    raw_code=(raw.get("caseNumber") or "").strip() or None,
                    raw_description=offence_text,
                    conviction_date=_parse_date(raw.get("dateOfOffence")),
                    jurisdiction="RW",
                    # Sentence is the closest source-published "level"; stored
                    # verbatim, never normalized across jurisdictions.
                    tier_or_level_raw=(raw.get("sentence") or "").strip() or None,
                )
            )

        # The registry only exposes numeric place codes (placeOfResidenceId)
        # with the human-readable locality left null, so there is no usable
        # street/city to populate. Mark the country only.
        addresses: list[Address] = [Address(type="home", country="RW")]

        status = _STATUS.get((raw.get("status") or "").strip().upper(), "active")

        return OffenderRecord(
            source=Source(
                jurisdiction="RW",
                source_id=str(raw.get("id")),
                source_url=f"{API}/offenders/{raw.get('id')}",
                info_url=f"{BASE}/",
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=Registration(status=status),
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # Portraits are inlined as base64 in `raw["picture"]`; the source serves
        # no image URL, so there is nothing URL-shaped to return here.
        return []


def build() -> RwRegistryAdapter:
    return RwRegistryAdapter()
