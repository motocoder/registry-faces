"""Nebraska state registry adapter — direct ASP.NET MVC scrape.

Source: https://sor.nebraska.gov

Nebraska State Patrol publishes its Sex Offender Registry as a plain
ASP.NET MVC application (jQuery + Kendo UI) with NO captcha and NO
acceptance gate. Three search modes:

  * `/Registry/NameSearch` → /Registry/Search?SearchType=Name&LastName=X
  * `/Registry/RegionSearch` → ZipCode / City / CountyId
  * `/Registry/LocationSearch` → radius

Detail pages live at `/Registry/Offender/<OffenderId>` (e.g.
`202412YI8`) and contain the rich per-person schema. Photos via
`/Image/<imageId>`.

Two-phase ingest:

  Phase 1 (enumerate): sweep `?LastName=A..Z&SearchType=Name` paginating
    each letter via `?page=N` (page size is hard-coded to 12 server-
    side). Collect all unique `OffenderId`s.

  Phase 2 (hydrate): fetch `/Registry/Offender/<id>` for every unique
    ID and parse the visible labels.

Schema notes:
  * `source_id` is the Nebraska-assigned OffenderId.
  * The detail page is HTML-only — labels are in `<div class="info_line">`
    and addresses appear under a section header. The parser is keyed to
    those structural anchors.
  * Photos: `<img src="/Image/<id>">` on the detail page (one or more).
    First image is treated as the primary mugshot.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from ..photos import PhotoRef
from ..schema import Address, Identity, Offense, OffenderRecord, Registration, Source
from .base import Adapter

BASE = "https://sor.nebraska.gov"
SEARCH_URL = f"{BASE}/Registry/Search"
DETAIL_BASE = f"{BASE}/Registry/Offender"
IMAGE_BASE = f"{BASE}/Image"
LETTERS = "abcdefghijklmnopqrstuvwxyz"

# Detail page label patterns
_LABEL_PATTERNS = {
    "dob": "Date of Birth",
    "registration_duration": "Registration Duration",
    "race": "Race",
    "sex": "Sex",
    "height": "Height",
    "weight": "Weight",
    "hair": "Hair",
    "eyes": "Eyes",
    "aliases": "Alias(s)",
}


@dataclass(frozen=True)
class _Hit:
    offender_id: str


class NebraskaAdapter(Adapter):
    jurisdiction = "US-NE"
    source_name = "Nebraska State Patrol"

    def __init__(
        self,
        request_timeout: float = 60.0,
        request_delay_s: float = 0.2,
        retry_attempts: int = 3,
        retry_backoff: float = 2.0,
        progress_every: int = 250,
    ) -> None:
        self.request_timeout = request_timeout
        self.request_delay_s = request_delay_s
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self.progress_every = progress_every
        self._fetched_at: datetime | None = None
        self._client: httpx.Client | None = None

    # ---- fetch --------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        self._fetched_at = datetime.now(timezone.utc)
        self._client = httpx.Client(
            timeout=self.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 registry-faces/0.1",
                "Accept": "text/html",
            },
        )
        try:
            seen: set[str] = set()
            print(f"  NE: enumerating across {len(LETTERS)} last-name letters", flush=True)
            for letter in LETTERS:
                for offender_id in self._enumerate_letter(letter):
                    if offender_id in seen:
                        continue
                    seen.add(offender_id)
                print(
                    f"    NE letter={letter.upper()}  unique_so_far={len(seen)}",
                    flush=True,
                )

            print(
                f"  NE: enumeration done. {len(seen)} unique offenders. "
                f"Hydrating detail pages.",
                flush=True,
            )
            hydrated = 0
            for offender_id in sorted(seen):
                payload = self._fetch_detail(offender_id)
                if payload is None:
                    continue
                hydrated += 1
                if hydrated % self.progress_every == 0:
                    print(f"    NE hydrated={hydrated}/{len(seen)}", flush=True)
                yield payload
        finally:
            self._client.close()
            self._client = None

    def _enumerate_letter(self, letter: str) -> Iterator[str]:
        """Page through ?SearchType=Name&LastName=<letter> until exhausted."""
        page = 1
        while True:
            html = self._get(
                SEARCH_URL,
                params={"SearchType": "Name", "LastName": letter, "page": page},
            )
            if html is None:
                return
            ids = _parse_result_ids(html)
            for oid in ids:
                yield oid
            if not ids:
                return
            total, end = _parse_pagination(html)
            if total is None or end is None or end >= total:
                return
            page += 1

    def _fetch_detail(self, offender_id: str) -> dict | None:
        html = self._get(f"{DETAIL_BASE}/{offender_id}")
        if html is None:
            return None
        return _parse_detail(offender_id, html)

    # ---- HTTP helpers -------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> str | None:
        assert self._client is not None
        last_err: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                r = self._client.get(url, params=params)
                r.raise_for_status()
                time.sleep(self.request_delay_s)
                return r.text
            except Exception as e:
                last_err = e
                time.sleep(self.retry_backoff * (attempt + 1))
        print(f"  NE GET fail {url} {params}: {last_err}", flush=True)
        return None

    # ---- normalize ----------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        offender_id = (raw.get("offender_id") or "").strip()
        title_name = (raw.get("title_name") or "").strip()
        # The title is the natural-language form ("First Middle Last").
        full_name = title_name or (raw.get("offender_id") or "UNKNOWN")

        dob = _parse_us_date(raw.get("dob"))
        height_cm = _height_to_cm(raw.get("height"))
        weight_kg = _weight_to_kg(raw.get("weight"))

        aliases_text = raw.get("aliases") or ""
        aliases = _split_aliases(aliases_text)

        identity = Identity(
            full_name=full_name,
            aliases=aliases,
            dob=dob,
            year_of_birth=dob.year if dob else None,
            sex=_sex(raw.get("sex")),
            race=(raw.get("race") or "").strip() or None,
            height_cm=height_cm,
            weight_kg=weight_kg,
            eye_color=(raw.get("eyes") or "").strip() or None,
            hair_color=(raw.get("hair") or "").strip() or None,
        )

        addresses: list[Address] = []
        for a in raw.get("addresses") or []:
            addr = _to_address(a)
            if addr is not None:
                addresses.append(addr)

        offenses: list[Offense] = []
        for o in raw.get("convictions") or []:
            desc = (o.get("crime") or "").strip()
            if not desc:
                continue
            offenses.append(
                Offense(
                    raw_description=desc,
                    raw_code=(o.get("statute") or "").strip() or None,
                    conviction_date=_parse_us_date(o.get("conviction_date")),
                    jurisdiction=(o.get("jurisdiction") or "").strip() or None,
                    statute=(o.get("statute") or "").strip() or None,
                )
            )

        # NE doesn't expose an explicit "absconder/incarcerated" status in
        # the detail HTML; everyone listed is by definition a current
        # registrant. Mark as active.
        registration = Registration(status="active", absconder=False)

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=offender_id,
                source_url=BASE,
                info_url=f"{DETAIL_BASE}/{offender_id}",
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # Take only the first /Image/<id> as the primary mugshot. NE serves
        # multiple historical photos for many records.
        for url in raw.get("photo_urls") or []:
            if isinstance(url, str) and url.startswith("http"):
                return [
                    PhotoRef(
                        url=url,
                        source_type="registry",
                        source_name=self.source_name,
                    )
                ]
        return []


# ---------------------------------------------------------------------------
# HTML parsing helpers


def _parse_result_ids(html: str) -> list[str]:
    """Pull /Registry/Offender/<id> anchors from a search results page."""
    s = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    seen: set[str] = set()
    for a in s.find_all("a", href=re.compile(r"/Registry/Offender/[0-9A-Z]+")):
        m = re.search(r"/Registry/Offender/([0-9A-Z]+)", a.get("href", ""))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def _parse_pagination(html: str) -> tuple[int | None, int | None]:
    """Return (total, end-of-range) from `Results N-M of T`."""
    m = re.search(r"results?\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)", html, re.I)
    if not m:
        return None, None
    return int(m.group(3)), int(m.group(2))


def _parse_detail(offender_id: str, html: str) -> dict:
    s = BeautifulSoup(html, "html.parser")

    # Title contains the natural-language name: "Nebraska Sex Offender Registry: Dino Anthony Alai"
    title_name = ""
    if s.title and s.title.text:
        m = re.search(r":\s*(.+)$", s.title.text.strip())
        if m:
            title_name = m.group(1).strip()

    # Field labels live in `<div class="info_line">LABEL:VALUE</div>`
    fields: dict[str, str] = {}
    for div in s.find_all("div", class_="info_line"):
        text = div.get_text(" ", strip=True)
        for key, label in _LABEL_PATTERNS.items():
            if text.startswith(label + ":") or text.startswith(label):
                value = text.split(":", 1)[1].strip() if ":" in text else ""
                if value and key not in fields:
                    fields[key] = value
                break

    # Photo URLs: <img src="/Image/<id>"> on the detail page.
    photo_urls: list[str] = []
    for img in s.find_all("img"):
        src = (img.get("src") or "").strip()
        if src.startswith("/Image/"):
            photo_urls.append(BASE + src)

    # Addresses: blocks under headers like "Physical/Main Address",
    # "Temporary Address", "Work Address". Each address block is a chunk
    # of text with street / city, state zip / county on consecutive lines
    # in the rendered HTML.
    addresses = _parse_addresses(s)

    # Sex Crime Convictions: a section header followed by Crime/Statute/
    # Jurisdiction/Court/Conviction Date/Place of Crime/Victim lines.
    convictions = _parse_convictions(s)

    return {
        "offender_id": offender_id,
        "title_name": title_name,
        **fields,
        "addresses": addresses,
        "convictions": convictions,
        "photo_urls": photo_urls,
    }


def _parse_addresses(s: BeautifulSoup) -> list[dict]:
    """Walk the addresses container, splitting on header text."""
    out: list[dict] = []
    # Look for a header element with text "Addresses", then walk siblings
    # until we hit "Schools"/"Vehicles"/"Sex Crime" etc.
    text = s.get_text("\n", strip=True)
    if "Addresses" not in text:
        return out
    # Heuristic: find each address block via header keywords.
    # Each block contains street line, then city/state/zip, then "<X> County".
    blocks = re.findall(
        r"(Physical/Main Address|Temporary Address|Work Address|Other Address|Mailing Address)"
        r"\s*\n([\s\S]{1,400}?)(?=(?:Physical/Main Address|Temporary Address|Work Address|"
        r"Other Address|Mailing Address|Offender attending|Schools|Vehicles|"
        r"Sex Crime Conviction|This map))",
        text,
    )
    for header, body in blocks:
        addr_type = (
            "home"
            if "Physical/Main" in header
            else "temporary"
            if "Temporary" in header
            else "work"
            if "Work" in header
            else "other"
        )
        # Body lines
        lines = [ln.strip() for ln in body.strip().split("\n") if ln.strip()]
        if not lines:
            continue
        street = lines[0] if lines else None
        city = state = zip_code = None
        county = None
        for ln in lines[1:]:
            csz = re.match(r"^(.+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", ln)
            if csz:
                city = csz.group(1).strip()
                state = csz.group(2)
                zip_code = csz.group(3)
                continue
            if ln.endswith("County"):
                county = ln.rsplit("County", 1)[0].strip()
                continue
        out.append(
            {
                "type": addr_type,
                "street": street,
                "city": city,
                "state": state,
                "zip": zip_code,
                "county": county,
            }
        )
    return out


def _parse_convictions(s: BeautifulSoup) -> list[dict]:
    """Pull each conviction block."""
    out: list[dict] = []
    text = s.get_text("\n", strip=True)
    if "Sex Crime Conviction" not in text:
        return out
    # Each conviction has Crime/Statute/Jurisdiction/Court/Date/Place/Victim
    # We capture by repeating the pattern after the section header.
    section = text.split("Sex Crime Conviction", 1)[1]
    # Slice off footers / unrelated tails.
    for terminator in ("This public notification", "Additional Images"):
        if terminator in section:
            section = section.split(terminator)[0]
    # Pattern: "Crime: ... Statute Number(s): ... Jurisdiction: ... Court: ... Conviction Date: ... Place of Crime: ... Victim of Crime: ..."
    # The labels each begin a new "line".
    blocks = re.findall(
        r"Crime:\s*([^\n]+)\n"
        r"(?:Statute Number\(s\):\s*([^\n]*)\n)?"
        r"(?:Jurisdiction:\s*([^\n]*)\n)?"
        r"(?:Court:\s*([^\n]*)\n)?"
        r"(?:Conviction Date:\s*([^\n]*)\n)?"
        r"(?:Place of Crime:\s*([^\n]*)\n)?"
        r"(?:Victim of Crime:\s*([^\n]*))?",
        section,
    )
    for crime, statute, jurisdiction, court, date, place, victim in blocks:
        out.append(
            {
                "crime": crime.strip(),
                "statute": statute.strip(),
                "jurisdiction": jurisdiction.strip(),
                "court": court.strip(),
                "conviction_date": date.strip(),
                "place_of_crime": place.strip(),
                "victim_of_crime": victim.strip(),
            }
        )
    return out


def _to_address(d: dict) -> Address | None:
    street = (d.get("street") or "").strip() or None
    city = (d.get("city") or "").strip() or None
    state = (d.get("state") or "").strip() or "NE"
    zip_code = (d.get("zip") or "").strip() or None
    if not any((street, city, zip_code)):
        return None
    return Address(
        type=d.get("type") or "home",  # type: ignore[arg-type]
        street=street,
        city=city,
        state=state,
        zip=zip_code,
    )


def _parse_us_date(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _sex(value: object) -> str:
    v = str(value or "").strip().lower()
    if v in {"m", "male"}:
        return "M"
    if v in {"f", "female"}:
        return "F"
    return "unknown"


def _height_to_cm(value: object) -> float | None:
    """NE writes height as `5' 8"`."""
    if not value:
        return None
    m = re.match(r"(\d+)'\s*(\d+)\"?", str(value).strip())
    if not m:
        return None
    feet, inches = int(m.group(1)), int(m.group(2))
    if not (3 <= feet <= 8) or not (0 <= inches <= 11):
        return None
    return round((feet * 12 + inches) * 2.54, 1)


def _weight_to_kg(value: object) -> float | None:
    if not value:
        return None
    m = re.match(r"(\d+)\s*lbs", str(value).strip(), re.I)
    if not m:
        return None
    pounds = int(m.group(1))
    if pounds <= 0 or pounds > 1000:
        return None
    return round(pounds * 0.45359237, 1)


def _split_aliases(text: str) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"[,;\n]", text) if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def build() -> NebraskaAdapter:
    return NebraskaAdapter()
