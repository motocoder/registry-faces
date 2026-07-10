"""Australia (Queensland) Daniel's Law missing-offender registry adapter.

Source type: official public HTML page backed by a public JSON feed.

Queensland's Daniel's Law site publishes a public "Missing Reportable
Offenders" page. The page's frontend JS fetches a static JSON payload with the
currently listed offenders. That feed is anonymous, official, and enumerable,
so this adapter targets it directly.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from registry_faces.adapters.base import Adapter
from registry_faces.photos import PhotoRef
from registry_faces.schema import Identity, OffenderRecord, Registration, Source

__all__ = ["AuRegistryAdapter", "build"]

BASE_URL = "https://www.danielslaw.qld.gov.au"
PAGE_URL = f"{BASE_URL}/daniels-law/missing-reportable-offenders/"
MAIN_JS_URL = f"{BASE_URL}/assets/js/main-new.js"
KNOWN_JSON_PATH = "/daniels-law/missing-reportable-offenders/QumQs0h6r9jW0bGa7zNc.json"
USER_AGENT = "registry-faces/1.0 (+Queensland Daniel's Law missing offenders)"

JSON_PATH_RE = re.compile(r'fetch\("(?P<path>/daniels-law/missing-reportable-offenders/[^"]+\.json)"')


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _year(value: object) -> int | None:
    text = _clean_str(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


class AuRegistryAdapter(Adapter):
    jurisdiction = "AU-QLD"
    source_name = "Daniel's Law Missing Reportable Offenders"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/javascript, */*;q=0.1"},
            timeout=60.0,
            follow_redirects=True,
        )

    def _discover_feed_url(self) -> str:
        resp = self._client.get(MAIN_JS_URL)
        resp.raise_for_status()
        match = JSON_PATH_RE.search(resp.text)
        if match:
            return urljoin(BASE_URL, match.group("path"))
        return urljoin(BASE_URL, KNOWN_JSON_PATH)

    def fetch(self) -> Iterator[dict]:
        feed_url = self._discover_feed_url()
        resp = self._client.get(feed_url)
        resp.raise_for_status()
        offenders = (resp.json() or {}).get("offenders") or []
        for row in sorted(offenders, key=lambda item: str(item.get("id") or "")):
            raw = dict(row)
            raw["_feed_url"] = feed_url
            raw["_page_url"] = PAGE_URL
            yield raw

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)
        full_name = _clean_str(raw.get("name")) or str(raw.get("id") or "").strip()
        aliases = [
            alias
            for alias in (_clean_str(value) for value in (raw.get("aliases") or []))
            if alias and alias != full_name
        ]

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=str(raw.get("id") or full_name),
                source_url=_clean_str(raw.get("_feed_url")),
                info_url=PAGE_URL,
                fetched_at=now,
            ),
            identity=Identity(
                full_name=full_name,
                aliases=aliases,
                year_of_birth=_year(raw.get("birthYear")),
                description="Missing reportable offender listed on Queensland's Daniel's Law public register.",
            ),
            registration=Registration(status="absconder", absconder=True),
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        img_path = _clean_str(raw.get("imgFileName"))
        if not img_path:
            return []
        return [
            PhotoRef(
                url=urljoin(BASE_URL, img_path),
                source_name=self.source_name,
                source_jurisdiction=self.jurisdiction,
                source_id=str(raw.get("id") or ""),
                caption=_clean_str(raw.get("name")),
            )
        ]


def build() -> AuRegistryAdapter:
    return AuRegistryAdapter()
