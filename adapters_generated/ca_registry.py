"""Canada — RCMP High Risk Child Sex Offender Database (HRCSODA).

Source type: paginated, server-rendered HTML (Drupal Views).

Canada's *National* Sex Offender Registry (NSOR) and the provincial registries
(Ontario OSOR, etc.) are police-only by law and are NOT public. The one
qualifying public, enumerable, government-run offender list is the RCMP's
High Risk Child Sex Offender Database, which publishes a browsable grid of
high-risk offenders the public has already been notified about:

    https://rcmp.ca/en/high-risk-child-sex-offender-database/search-database

The landing view lists ~15 profile cards per page (name + province + photo)
and paginates via `?page=N`. Each card links to a detail page carrying
gender / height / weight / eye colour / hair / offence summary / conditions.

The detail page inlines the portrait as a base64 data-URI (anti-scrape), but
each *listing card* serves a real photo URL under `/sites/default/files/...`,
so `extract_photos` returns that source-served URL only.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urljoin

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
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://rcmp.ca"
LISTING = f"{BASE}/en/high-risk-child-sex-offender-database/search-database"
USER_AGENT = "registry-faces/1.0 (+https://rcmp.ca public HRCSODA index)"

# Safety bound: the database holds a handful of high-risk offenders nationally.
# Stop on the first empty page; this cap just prevents a runaway loop.
MAX_PAGES = 50

_SEX = {"male": "M", "female": "F", "other": "X", "unknown": "unknown"}


def _gender(value: str | None) -> str:
    return _SEX.get((value or "").strip().lower(), "unknown")


def _leading_number(value: str | None) -> float | None:
    """Pull the first number from strings like '180 cm (5 ft 11 in )'."""
    if not value:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    return float(m.group()) if m else None


class CaRegistryAdapter(Adapter):
    jurisdiction = "CA"
    source_name = "RCMP High Risk Child Sex Offender Database"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        )

    # -- fetch ----------------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        for page in range(MAX_PAGES):
            resp = self._client.get(LISTING, params={"page": page})
            resp.raise_for_status()
            cards = self._parse_cards(resp.text)
            if not cards:
                break
            for card in cards:
                detail = self._fetch_detail(card["source_url"])
                yield {**card, **detail}

    def _parse_cards(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        cards: list[dict] = []
        for card in soup.select(".hrcsoda-profile-card"):
            inner = card.select_one("article.hrcsoda-profile-card__inner")
            link = card.select_one("a.hrcsoda-profile-card__link")
            if inner is None or link is None:
                continue
            href = link.get("href", "")
            source_id = href.rstrip("/").rsplit("/", 1)[-1]
            img = card.select_one("img")
            photo = img.get("src") if img else None
            cards.append(
                {
                    "source_id": source_id,
                    "name": (inner.get("data-name") or "").strip(),
                    "province": (inner.get("data-province") or "").strip(),
                    "source_url": urljoin(BASE, href),
                    "photo_url": urljoin(BASE, photo) if photo else None,
                }
            )
        return cards

    def _fetch_detail(self, url: str) -> dict:
        resp = self._client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        main = soup.select_one("main") or soup

        fields: dict[str, str] = {}
        for dt in main.select("dt"):
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                fields[dt.get_text(" ", strip=True).rstrip(":")] = dd.get_text(
                    " ", strip=True
                )

        sections: dict[str, str] = {}
        for heading in main.select("h2, h3"):
            label = heading.get_text(" ", strip=True)
            parts: list[str] = []
            node = heading.find_next_sibling()
            while node is not None and node.name not in ("h2", "h3"):
                text = node.get_text(" ", strip=True)
                if text:
                    parts.append(text)
                node = node.find_next_sibling()
            sections[label] = " ".join(parts)

        return {"fields": fields, "sections": sections}

    # -- normalize ------------------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        fields: dict = raw.get("fields", {})
        sections: dict = raw.get("sections", {})
        now = datetime.now(timezone.utc)

        identity = Identity(
            full_name=raw["name"],
            sex=_gender(fields.get("Gender")),
            height_cm=_leading_number(fields.get("Height")),
            weight_kg=_leading_number(fields.get("Weight")),
            eye_color=fields.get("Eye colour") or None,
            hair_color=fields.get("Hair style") or fields.get("Hair colour") or None,
            description=sections.get("Conditions") or None,
        )

        addresses: list[Address] = []
        residence = (sections.get("Place of residence") or "").strip()
        if raw.get("province") or residence:
            addresses.append(
                Address(
                    type="home",
                    city=residence or None,
                    state=raw.get("province") or None,
                    country="CA",
                )
            )

        offenses: list[Offense] = []
        offence_text = (sections.get("Description of offences") or "").strip()
        if offence_text:
            offenses.append(
                Offense(raw_description=offence_text, jurisdiction="CA")
            )

        return OffenderRecord(
            source=Source(
                jurisdiction="CA",
                source_id=raw["source_id"],
                source_url=raw.get("source_url"),
                info_url=LISTING,
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=Registration(status="active"),
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = raw.get("photo_url")
        if not url:
            return []
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
                caption=raw.get("name") or None,
            )
        ]


def build() -> CaRegistryAdapter:
    return CaRegistryAdapter()
