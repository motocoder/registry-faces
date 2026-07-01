"""Guam (Judiciary of Guam) public sex offender registry adapter.

Source: https://soregistry.guamcourts.gov/

The Judiciary of Guam Probation Services Division publishes the territory's
sex offender registry as a public, enumerable directory — no login and no
CAPTCHA on the listing surface. The directory lives at
`/homes/sor_directory` (a CakePHP app) and paginates 10 records per page via
`/homes/sor_directory/page:N`. The per-page size is a POST-bound select that
ignores query overrides, so we sweep pages sequentially until one yields no
records.

Each list card is fully populated — name, stable detail id, offender status,
level, date of birth, ethnicity, conviction text, and a registry-served photo
thumbnail — so the detail page (`/homes/sor_directory_details/<id>`) adds
nothing the listing doesn't already carry. We parse the listing only.

Photo thumbnails are served from `/uploads/offenders/thumbs/<filename>`; the
filenames embed spaces and commas, so the path is percent-encoded.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from registry_faces.schema import (
    Identity,
    Offense,
    OffenderRecord,
    Registration,
    Source,
)
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE_URL = "https://soregistry.guamcourts.gov"
DIRECTORY_URL = BASE_URL + "/homes/sor_directory"
DETAIL_URL_TEMPLATE = BASE_URL + "/homes/sor_directory_details/{source_id}"
# Defensive page cap: ~860 records / 10 per page ≈ 86 pages. The loop stops
# on the first empty page; this bound only guards against a pagination bug.
MAX_PAGES = 500

_ID_RE = re.compile(r"sor_directory_details/([A-Za-z0-9-]+)")


class GuamRegistryAdapter(Adapter):
    jurisdiction = "GU"
    source_name = "Judiciary of Guam — Sex Offender Registry"

    def __init__(self, directory_url: str = DIRECTORY_URL) -> None:
        self.directory_url = directory_url
        self._fetched_at: datetime | None = None

    def fetch(self) -> Iterator[dict]:
        self._fetched_at = datetime.now(timezone.utc)
        seen: set[str] = set()
        with httpx.Client(
            follow_redirects=True,
            timeout=120,
            headers={"User-Agent": "Mozilla/5.0 registry-faces/0.1"},
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                url = f"{self.directory_url}/page:{page}"
                resp = client.get(url)
                resp.raise_for_status()
                cards = list(self._parse_cards(resp.text))
                fresh = [c for c in cards if c["source_id"] not in seen]
                if not fresh:
                    # Empty page, or a tail page repeating the last batch.
                    break
                for card in fresh:
                    seen.add(card["source_id"])
                    yield card

    @staticmethod
    def _parse_cards(html: str) -> Iterator[dict]:
        soup = BeautifulSoup(html, "lxml")
        for article in soup.find_all("article"):
            id_link = article.find("a", href=_ID_RE)
            if not id_link:
                continue
            source_id = _ID_RE.search(id_link["href"]).group(1)

            name = None
            title = article.find(class_="post-title")
            if title:
                name = title.get_text(" ", strip=True)

            photo_url = None
            img = article.find("img")
            if img and img.get("src"):
                photo_url = _abs_photo_url(img["src"])

            status = None
            status_label = article.find(
                "strong", string=re.compile(r"Offender Status", re.I)
            )
            if status_label and status_label.parent:
                status = _strip_label(status_label.parent.get_text(" ", strip=True))

            fields = _list_fields(article)

            offense = None
            offense_box = article.find(class_="contxt1")
            if offense_box:
                offense = offense_box.get_text(" ", strip=True) or None

            yield {
                "source_id": source_id,
                "name": name,
                "photo_url": photo_url,
                "offender_status": status,
                "level": fields.get("level"),
                "date_of_birth": fields.get("date of birth"),
                "ethnicity": fields.get("ethnicity"),
                "offense": offense,
                "detail_url": DETAIL_URL_TEMPLATE.format(source_id=source_id),
            }

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        full_name = (raw.get("name") or "UNKNOWN").strip() or "UNKNOWN"
        dob = _parse_dob(raw.get("date_of_birth"))

        offenses: list[Offense] = []
        if raw.get("offense"):
            offenses.append(
                Offense(
                    raw_description=raw["offense"],
                    jurisdiction="GU",
                    tier_or_level_raw=raw.get("level"),
                )
            )

        return OffenderRecord(
            source=Source(
                jurisdiction="GU",
                source_id=str(raw["source_id"]),
                source_url=self.directory_url,
                info_url=raw.get("detail_url"),
                fetched_at=self._fetched_at,
            ),
            identity=Identity(
                full_name=full_name,
                dob=dob,
                year_of_birth=dob.year if dob else None,
                race=raw.get("ethnicity"),
            ),
            offenses=offenses,
            registration=Registration(status=_map_status(raw.get("offender_status"))),
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = raw.get("photo_url")
        if not url:
            return []
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


def _list_fields(article) -> dict[str, str]:
    """Pull the <li><strong>Label:</strong> value</li> pairs into a dict."""
    out: dict[str, str] = {}
    for li in article.find_all("li"):
        label = li.find("strong")
        if not label:
            continue
        key = label.get_text(strip=True).rstrip(":").strip().lower()
        value = _strip_label(li.get_text(" ", strip=True))
        if value:
            out[key] = value
    return out


def _strip_label(text: str) -> str | None:
    """Drop the leading 'Label:' from a 'Label: value' string."""
    value = text.split(":", 1)[1].strip() if ":" in text else text.strip()
    return value or None


def _abs_photo_url(src: str) -> str:
    full = urljoin(BASE_URL + "/", src.lstrip("/"))
    # Percent-encode the path (filenames carry spaces/commas) without
    # touching the scheme/host or any existing query.
    scheme, _, rest = full.partition("://")
    host, _, path = rest.partition("/")
    return f"{scheme}://{host}/{quote(path)}"


def _parse_dob(value: object) -> datetime | None:
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", str(value))
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _map_status(value: object) -> str:
    """Map Guam's free-text status onto the canonical RegistrationStatus.

    The verbatim source string is preserved in `raw`; this is only the
    coarse bucket. Unknown/absent strings fall through to 'unknown'.
    """
    if not value:
        return "unknown"
    s = str(value).lower()
    if "deceas" in s:
        return "deceased"
    if "incarcerat" in s or "corrections" in s or "doc" in s or "in jail" in s:
        return "incarcerated"
    if "abscond" in s or "non-compliant" in s or "noncompliant" in s or "failed" in s:
        return "absconder"
    if "active" in s or "supervis" in s or "registered" in s or "compliant" in s:
        return "active"
    return "unknown"


def build() -> GuamRegistryAdapter:
    return GuamRegistryAdapter()
