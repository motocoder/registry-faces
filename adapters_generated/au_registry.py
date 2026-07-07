"""Australia (Queensland) Daniel's Law missing offenders adapter.

Source type: public JSON feed behind a JS-rendered government page.

Queensland's official Daniel's Law site publishes a public "Missing Reportable
Offenders" webpage. The page JavaScript fetches an enumerable JSON feed of
missing, non-compliant reportable offenders:

  https://www.danielslaw.qld.gov.au/daniels-law/missing-reportable-offenders/
  https://www.danielslaw.qld.gov.au/daniels-law/missing-reportable-offenders/QumQs0h6r9jW0bGa7zNc.json

Each record includes a stable QP number, full name, aliases, year of birth,
and a source-served photo path rendered from /assets/images/<imgFileName>.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from registry_faces.schema import OffenderRecord, Source, Identity, Address, Offense
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://www.danielslaw.qld.gov.au"
LISTING_URL = f"{BASE}/daniels-law/missing-reportable-offenders/"
JSON_URL = f"{LISTING_URL}QumQs0h6r9jW0bGa7zNc.json"
USER_AGENT = "registry-faces/1.0 (+public Queensland Daniel's Law index)"


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _aliases(raw: dict, full_name: str) -> list[str]:
    aliases: list[str] = []
    seen = {full_name.casefold()} if full_name else set()
    for value in raw.get("aliases") or []:
        alias = _clean(value)
        if not alias:
            continue
        folded = alias.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        aliases.append(alias)
    return aliases


def _year_of_birth(value: object) -> int | None:
    text = _clean(value)
    if len(text) != 4 or not text.isdigit():
        return None
    year = int(text)
    return year if 1900 <= year <= 2100 else None


def _photo_url(raw: dict) -> str | None:
    path = _clean(raw.get("imgFileName"))
    if not path:
        return None
    return urljoin(f"{BASE}/", f"assets/images/{path.lstrip('/')}")


class AuRegistryAdapter(Adapter):
    jurisdiction = "AU-QLD"
    source_name = "Queensland Daniel's Law Missing Reportable Offenders"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/json"},
            timeout=60.0,
            follow_redirects=True,
        )

    def fetch(self) -> Iterator[dict]:
        resp = self._client.get(JSON_URL)
        resp.raise_for_status()
        payload = resp.json()
        offenders = payload.get("offenders") or []

        deduped: dict[str, dict] = {}
        for row in offenders:
            source_id = _clean(row.get("id"))
            if not source_id:
                continue
            deduped[source_id] = row

        for source_id in sorted(deduped):
            yield deduped[source_id]

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)
        full_name = _clean(raw.get("name"))
        source_id = _clean(raw.get("id"))

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=source_id,
                source_url=JSON_URL,
                info_url=LISTING_URL,
                fetched_at=now,
            ),
            identity=Identity(
                full_name=full_name,
                aliases=_aliases(raw, full_name),
                year_of_birth=_year_of_birth(raw.get("birthYear")),
            ),
            addresses=[],
            offenses=[
                Offense(
                    raw_description="Missing reportable offender listing under Queensland Daniel's Law",
                    jurisdiction=self.jurisdiction,
                )
            ],
            registration={"status": "absconder", "absconder": True},
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = _photo_url(raw)
        if not url:
            return []
        return [
            PhotoRef(
                url=url,
                source_name=self.source_name,
                source_jurisdiction=self.jurisdiction,
                source_id=_clean(raw.get("id")),
                caption=_clean(raw.get("name")) or None,
            )
        ]


def build() -> AuRegistryAdapter:
    return AuRegistryAdapter()
