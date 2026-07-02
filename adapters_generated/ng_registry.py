"""Nigeria — NAPTIP Sexual Offenders Database (NSOD), convicted register.

Source type: server-rendered HTML (Laravel/Blade), scraped with BeautifulSoup.

Nigeria is one of the rare non-US jurisdictions that runs an OFFICIAL, public,
enumerable sex-offender registry. The National Agency for the Prohibition of
Trafficking in Persons (NAPTIP) publishes the convicted-offender register at:

    https://nsod.naptip.gov.ng/convicts

The public "Search Convicted Cases" page is a GET form with two filters,
``nameQuery`` and ``prosecutionStateQuery`` (a numeric state id). With an empty
name and a single state id the server returns every convicted record prosecuted
in that state, each rendered as a ``div.offender-card`` carrying the full record
in ``data-*`` attributes (name, offence, judgement, photo, sex, nationality,
address, NIN, court, sentence). Sweeping the ~37 state options of the
``prosecutionStateQuery`` select therefore enumerates the entire convicted
register (a few hundred records) without any name guessing.

Photos: each card's ``data-photo`` is either a real portrait served from the
registry's own ``/storage/`` path or a built-in ``/assets/media/avatars/``
placeholder. ``extract_photos`` returns only the real source-served URLs.

The registry has no stable per-offender id in the public payload, so
``source_id`` is a deterministic SHA1 over the immutable record fields.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from registry_faces.schema import (
    Address,
    Identity,
    Offense,
    OffenderRecord,
    Registration,
    Source,
)
from web_scrubber.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://nsod.naptip.gov.ng"
CONVICTS = f"{BASE}/convicts"
USER_AGENT = "registry-faces/1.0 (+NAPTIP NSOD public convicted-offender index)"

# Cards whose portrait points here are the template's default avatar, not a
# real source-published photo.
_PLACEHOLDER_PHOTO = "/assets/media/avatars/"

_SEX = {"MALE": "M", "FEMALE": "F"}

_CARD_FIELDS = (
    "name",
    "offence",
    "judgement",
    "photo",
    "sex",
    "nationality",
    "location",
    "nin",
    "court_name",
    "court_state",
    "phone_no",
    "sentence",
)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _sex(value: str | None) -> str:
    return _SEX.get((value or "").strip().upper(), "unknown")


def _source_id(raw: dict) -> str:
    # No stable id is exposed publicly; derive one from the immutable record
    # fields so re-ingest of an unchanged record maps to the same person.
    key = "|".join(
        (raw.get(field) or "").strip()
        for field in ("name", "offence", "court_name", "court_state", "sentence")
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class NgRegistryAdapter(Adapter):
    jurisdiction = "NG"
    source_name = "NAPTIP Nigeria Sexual Offenders Database (NSOD)"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
        )

    # -- fetch ----------------------------------------------------------------

    def _state_ids(self) -> list[str]:
        """Read the prosecution-state filter options from the search page."""
        resp = self._client.get(CONVICTS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        select = soup.select_one("select[name=prosecutionStateQuery]")
        ids: list[str] = []
        if select is not None:
            for option in select.find_all("option"):
                value = (option.get("value") or "").strip()
                if value:
                    ids.append(value)
        return ids

    @staticmethod
    def _parse_cards(html: str) -> Iterator[dict]:
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select(".offender-card"):
            raw = {field: (card.get(f"data-{field}") or "").strip() for field in _CARD_FIELDS}
            if raw.get("name"):
                yield raw

    def fetch(self) -> Iterator[dict]:
        seen: set[str] = set()
        for state_id in self._state_ids():
            resp = self._client.get(
                CONVICTS, params={"nameQuery": "", "prosecutionStateQuery": state_id}
            )
            resp.raise_for_status()
            for raw in self._parse_cards(resp.text):
                sid = _source_id(raw)
                if sid in seen:
                    continue
                seen.add(sid)
                raw["_prosecution_state_id"] = state_id
                yield raw

    # -- normalize ------------------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)

        nationality = _clean(raw.get("nationality"))
        identity = Identity(
            full_name=(raw.get("name") or "").strip(),
            sex=_sex(raw.get("sex")),
            description=f"Nationality: {nationality}" if nationality else None,
        )

        # data-location is the offender's residence (often blank); the
        # prosecution state lives separately on the offence side.
        addresses: list[Address] = []
        location = _clean(raw.get("location"))
        if location:
            addresses.append(Address(type="home", street=location, country="NG"))

        offenses: list[Offense] = []
        offence_text = _clean(raw.get("offence"))
        if offence_text:
            court = _clean(raw.get("court_name"))
            court_state = _clean(raw.get("court_state"))
            offenses.append(
                Offense(
                    raw_description=offence_text,
                    jurisdiction="NG",
                    statute=", ".join(p for p in (court, court_state) if p) or None,
                    # Sentence is the closest source-published "level"; stored
                    # verbatim, never normalized across jurisdictions.
                    tier_or_level_raw=_clean(raw.get("sentence")),
                )
            )

        return OffenderRecord(
            source=Source(
                jurisdiction="NG",
                source_id=_source_id(raw),
                source_url=CONVICTS,
                info_url=f"{BASE}/",
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            # Every record on this surface is a published conviction.
            registration=Registration(status="active"),
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = _clean(raw.get("photo"))
        if not url or _PLACEHOLDER_PHOTO in url:
            return []
        if url.startswith("/"):
            url = f"{BASE}{url}"
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
                caption=_clean(raw.get("name")),
            )
        ]


def build() -> NgRegistryAdapter:
    return NgRegistryAdapter()
