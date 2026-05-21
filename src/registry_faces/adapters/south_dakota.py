"""South Dakota DCI sex offender registry adapter.

Source: https://sor.sd.gov

The public search UI is powered by an unauthenticated JSON endpoint at
`POST /Offenders/Search` that returns the full registry (one record per
offender per address — some people appear twice when they have a primary
plus a secondary address). Records include geocoded lat/lon and photo
filenames. Photos are served at `/Offenders/Photo?fileName=<filename>`.

The browser flow loads a Google reCAPTCHA, but the JSON endpoint itself
does not currently enforce it. If that changes, the adapter would have to
solve a captcha — at which point the right move is to stop and report,
not to integrate a captcha-bypass service.
"""

import re
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, OffenderRecord, Registration, Source
from .base import Adapter

SEARCH_URL = "https://sor.sd.gov/Offenders/Search"
# `/Offenders/Photo?fileName=...` returns the SPA shell HTML — it isn't the
# real endpoint. The actual JPEG is served from /sorfiles/OffenderImages/.
PHOTO_URL_TEMPLATE = "https://sor.sd.gov/sorfiles/OffenderImages/{filename}"
# Public detail page for the offender. Requires the disclaimer-accept cookie
# in a browser (set by clicking Continue on the homepage); the URL itself is
# stable per-Id.
INFO_URL_TEMPLATE = "https://sor.sd.gov/Offenders/Details?id={source_id}"


class SouthDakotaAdapter(Adapter):
    jurisdiction = "US-SD"
    source_name = "South Dakota Division of Criminal Investigation"

    def __init__(self, search_url: str = SEARCH_URL) -> None:
        self.search_url = search_url
        self._fetched_at: datetime | None = None

    def fetch(self) -> Iterator[dict]:
        self._fetched_at = datetime.now(timezone.utc)
        with httpx.Client(
            follow_redirects=True,
            timeout=120,
            headers={
                "User-Agent": "Mozilla/5.0 registry-faces/0.1",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        ) as client:
            resp = client.post(self.search_url, json={})
            resp.raise_for_status()
            data = resp.json()
        for record in data.get("Results", []):
            yield record

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        full_name = (
            raw.get("FullName")
            or " ".join(p for p in (raw.get("FirstName"), raw.get("LastName")) if p).strip()
            or "UNKNOWN"
        )
        source_id = str(raw["Id"])

        dob = _parse_dob(raw.get("DateOfBirth"))
        year_of_birth = dob.year if dob else None

        address = Address(
            type="home",
            street=raw.get("Address"),
            city=raw.get("City"),
            state="SD",
            zip=raw.get("ZipCode"),
            lat=raw.get("Latitude"),
            lon=raw.get("Longitude"),
        )

        registration = Registration(
            status="incarcerated" if raw.get("IsInJail") else "active",
        )

        return OffenderRecord(
            source=Source(
                jurisdiction="US-SD",
                source_id=source_id,
                source_url=self.search_url,
                info_url=INFO_URL_TEMPLATE.format(source_id=source_id),
                fetched_at=self._fetched_at,
            ),
            identity=Identity(
                full_name=full_name,
                dob=dob,
                year_of_birth=year_of_birth,
            ),
            addresses=[address],
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        filename = raw.get("ImageFileName")
        if not filename:
            return []
        return [
            PhotoRef(
                url=PHOTO_URL_TEMPLATE.format(filename=filename),
                source_type="registry",
                source_name=self.source_name,
            )
        ]


def _parse_dob(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def build() -> SouthDakotaAdapter:
    return SouthDakotaAdapter()
