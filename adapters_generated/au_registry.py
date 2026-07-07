"""Australia (Queensland) public child sex offender register adapter.

Source type: static JSON feed behind an official Queensland Government site.

Queensland's Daniel's Law site exposes a public "Missing Reportable Offenders"
page for reportable offenders who have breached reporting obligations and whose
whereabouts are unknown to police. The page is backed by a stable JSON file:

    https://www.danielslaw.qld.gov.au/daniels-law/missing-reportable-offenders/QumQs0h6r9jW0bGa7zNc.json

Each record publishes a full name, aliases, year of birth, a QP reference
number, and an image filename under `/assets/images/offenders/`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from registry_faces.adapters.base import Adapter
from registry_faces.photos import PhotoRef
from registry_faces.schema import Identity, OffenderRecord, Source

BASE = "https://www.danielslaw.qld.gov.au"
PAGE_URL = f"{BASE}/daniels-law/missing-reportable-offenders/"
JSON_URL = f"{PAGE_URL}QumQs0h6r9jW0bGa7zNc.json"
IMAGE_BASE = f"{BASE}/assets/images/"
USER_AGENT = "registry-faces/1.0 (+public Queensland Daniel's Law register)"


def _clean(value) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _year(value) -> int | None:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


class AuRegistryAdapter(Adapter):
    jurisdiction = "AU-QLD"
    source_name = "Queensland Daniel's Law Missing Reportable Offenders"

    def fetch(self) -> Iterator[dict]:
        response = httpx.get(
            JSON_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            timeout=60.0,
        )
        response.raise_for_status()
        payload = response.json()
        offenders = payload.get("offenders") or []
        for raw in sorted(offenders, key=lambda item: str(item.get("id") or item.get("name") or "")):
            yield raw

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)
        name = _clean(raw.get("name")) or _clean(raw.get("id")) or "Unknown"

        aliases: list[str] = []
        for value in raw.get("aliases") or []:
            cleaned = _clean(value)
            if cleaned:
                aliases.append(cleaned)

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=_clean(raw.get("id")) or name,
                source_url=JSON_URL,
                info_url=PAGE_URL,
                fetched_at=now,
            ),
            identity=Identity(
                full_name=name,
                aliases=aliases,
                year_of_birth=_year(raw.get("birthYear")),
                sex="unknown",
            ),
            registration={"status": "absconder", "absconder": True},
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        img_file = _clean(raw.get("imgFileName"))
        if not img_file:
            return []
        return [PhotoRef(url=urljoin(IMAGE_BASE, img_file))]


def build() -> AuRegistryAdapter:
    return AuRegistryAdapter()
