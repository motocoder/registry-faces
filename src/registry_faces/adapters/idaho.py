"""Idaho ISP sex offender registry adapter.

Source: https://apps.isp.idaho.gov/sor_id/

Plain HTML forms with paginated server-rendered search results — no
captcha, no auth, no SPA. Two-phase ingest:

  Phase 1 (enumerate): POST /sor_id/SOR with each of the 327 published
    Idaho ZIPs, both Adult (rad=A) and Juvenile (rad=J). Server caps
    each page at ~15 records regardless of `sz`, so we follow the
    pagination links until no `Next>>`. KNOs deduped across all zips.

  Phase 2 (hydrate): GET /sor_id/SOR?id=<numeric internal id> for every
    unique offender to read the full schema (DOB, race, height, weight,
    aliases, offenses, photo URL).

The internal `id=` is shown in the search-results detail-page link;
the public `KNO` (e.g. 8012418) is the stable per-offender identifier
and becomes our `source.source_id`.

Idaho's TOU (`Idaho Code 74-120`) forbids commercial use and using the
data to compile telephone/mailing lists. The adapter is plain-HTTP and
respects that scope; the disclosure is on the user.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from ..photos import PhotoRef
from ..schema import Address, Identity, Offense, OffenderRecord, Registration, Source
from .base import Adapter

BASE = "https://apps.isp.idaho.gov/sor_id"
SOR_URL = f"{BASE}/SOR"
ZIPS_URL = f"{BASE}/zip.html"

# Reg Status text → canonical Registration.status.
_STATUS_MAP = {
    "COMPLIANT": "active",
    "NON COMP": "active",  # still registered, just non-compliant — not an absconder per Idaho
    "NONCOMP": "active",
    "PENDING": "active",
    "VSP": "active",
    "ABSCONDED": "absconder",
    "ABSCONDER": "absconder",
    "DECEASED": "deceased",
    "INCARCERATED": "incarcerated",
}


@dataclass(frozen=True)
class _Hit:
    """A row from a search-results page. Holds the minimum needed to
    dedup and to re-fetch the detail page."""
    kno: str
    internal_id: str
    name: str
    address: str
    city: str
    county: str
    zip_code: str
    status: str


class IdahoAdapter(Adapter):
    jurisdiction = "US-ID"
    source_name = "Idaho State Police - Sex Offender Registry"

    def __init__(
        self,
        request_timeout: float = 60.0,
        request_delay_s: float = 0.15,
        retry_attempts: int = 3,
        progress_every: int = 250,
        do_juvenile: bool = True,
    ) -> None:
        self.request_timeout = request_timeout
        self.request_delay_s = request_delay_s
        self.retry_attempts = retry_attempts
        self.progress_every = progress_every
        self.do_juvenile = do_juvenile
        self._fetched_at: datetime | None = None
        self._client: httpx.Client | None = None
        # Holds the most recent enumerate-pass hits keyed by KNO so
        # normalize() can fall back on the search-row data when a detail
        # page is unavailable.
        self._hits_by_kno: dict[str, _Hit] = {}

    # ---- fetch ---------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        self._fetched_at = datetime.now(timezone.utc)
        self._client = httpx.Client(
            timeout=self.request_timeout,
            headers={
                "User-Agent": "Mozilla/5.0 registry-faces/0.1",
                "Accept": "text/html",
            },
            follow_redirects=True,
        )
        try:
            zips = self._list_zips()
            print(f"  ID: enumerating across {len(zips)} ZIPs", flush=True)
            seen: set[str] = set()
            yielded = 0
            zips_done = 0
            for z in zips:
                for rad in (("A", "J") if self.do_juvenile else ("A",)):
                    for hit in self._iter_zip_results(z, rad):
                        if hit.kno in seen:
                            continue
                        seen.add(hit.kno)
                        self._hits_by_kno[hit.kno] = hit
                zips_done += 1
                if zips_done % 25 == 0:
                    print(
                        f"    ID zips={zips_done}/{len(zips)}  unique_offenders={len(seen)}",
                        flush=True,
                    )

            print(
                f"  ID: enumeration done. {len(seen)} unique offenders. "
                f"Hydrating detail pages.",
                flush=True,
            )
            for kno in sorted(seen):
                detail = self._fetch_detail(kno)
                if detail is None:
                    # Fall back to the search-row data so the person isn't lost.
                    hit = self._hits_by_kno[kno]
                    detail = _hit_only_payload(hit)
                yielded += 1
                if yielded % self.progress_every == 0:
                    print(f"    ID hydrated={yielded}/{len(seen)}", flush=True)
                yield detail
        finally:
            self._client.close()
            self._client = None

    # ---- enumeration ---------------------------------------------------

    def _list_zips(self) -> list[str]:
        html = self._get(ZIPS_URL)
        s = BeautifulSoup(html, "html.parser")
        out = []
        seen = set()
        for opt in s.find_all("option"):
            v = (opt.get("value") or "").strip()
            if v and v.isdigit() and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def _iter_zip_results(self, zip_code: str, rad: str) -> Iterator[_Hit]:
        page = 1
        while True:
            html = self._post(
                SOR_URL,
                {
                    "form": "5",
                    "zip": zip_code,
                    "rad": rad,
                    "sz": "400",
                    "page": str(page),
                },
            )
            if html is None:
                return
            hits = _parse_results(html, default_zip=zip_code)
            for h in hits:
                yield h
            if not _has_next_page(html, page):
                return
            page += 1

    # ---- detail --------------------------------------------------------

    def _fetch_detail(self, kno: str) -> dict | None:
        """Walk from KNO to detail page. We need the internal id= for the
        detail URL; it was captured at enumerate time."""
        hit = self._hits_by_kno.get(kno)
        if hit is None or not hit.internal_id:
            return None
        html = self._get(f"{SOR_URL}?id={hit.internal_id}&sz=400")
        if html is None:
            return None
        return _parse_detail(html, hit)

    # ---- HTTP helpers --------------------------------------------------

    def _get(self, url: str) -> str | None:
        assert self._client is not None
        last_err: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                r = self._client.get(url)
                r.raise_for_status()
                time.sleep(self.request_delay_s)
                return r.text
            except Exception as e:
                last_err = e
                time.sleep(1.0 + attempt)
        print(f"  ID GET fail {url}: {last_err}", flush=True)
        return None

    def _post(self, url: str, data: dict) -> str | None:
        assert self._client is not None
        last_err: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                r = self._client.post(url, data=data)
                r.raise_for_status()
                time.sleep(self.request_delay_s)
                return r.text
            except Exception as e:
                last_err = e
                time.sleep(1.0 + attempt)
        print(f"  ID POST fail {url} {data}: {last_err}", flush=True)
        return None

    # ---- normalize -----------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        kno = str(raw.get("kno") or "")
        name = (raw.get("name") or "UNKNOWN").strip() or "UNKNOWN"
        full_name = _flip_lastfirst(name)

        aliases = [a.strip() for a in (raw.get("aliases") or []) if a and a.strip()]

        dob = _parse_long_date(raw.get("birth_date"))
        sex = _sex(raw.get("sex"))
        height_cm = _height_to_cm(raw.get("height"))
        weight_kg = _weight_to_kg(raw.get("weight"))

        identity = Identity(
            full_name=full_name,
            aliases=aliases,
            dob=dob,
            year_of_birth=dob.year if dob else None,
            sex=sex,
            race=(raw.get("race") or "").strip() or None,
            height_cm=height_cm,
            weight_kg=weight_kg,
            eye_color=(raw.get("eye_color") or "").strip() or None,
            hair_color=(raw.get("hair_color") or "").strip() or None,
        )

        addresses: list[Address] = []
        addr = (raw.get("address") or "").strip()
        city = (raw.get("city") or "").strip()
        county = (raw.get("county") or "").strip()
        zip_code = (raw.get("zip_code") or "").strip()
        if addr or city or zip_code:
            addresses.append(
                Address(
                    type="home",
                    street=addr or None,
                    city=city or None,
                    state="ID",
                    zip=zip_code or None,
                )
            )
            _ = county  # preserved in raw

        offenses: list[Offense] = []
        for o in raw.get("offenses") or []:
            desc = (o.get("description") or "").strip()
            if not desc:
                continue
            offenses.append(
                Offense(
                    raw_code=(o.get("statute") or "").strip() or None,
                    raw_description=desc,
                    conviction_date=_parse_long_date(o.get("date")),
                    jurisdiction=(o.get("jurisdiction") or "").strip() or None,
                    statute=(o.get("statute") or "").strip() or None,
                )
            )

        status_raw = (raw.get("status") or "").strip().upper()
        status = _STATUS_MAP.get(status_raw, "active" if status_raw else "unknown")
        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=(status == "absconder"),
        )

        info_url = None
        if raw.get("internal_id"):
            info_url = f"{SOR_URL}?id={raw['internal_id']}"
        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=kno,
                source_url=BASE,
                info_url=info_url,
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = (raw.get("photo_url") or "").strip()
        if not url:
            return []
        if url.startswith("/"):
            url = f"https://apps.isp.idaho.gov{url}"
        elif url.startswith("../"):
            # On the detail page the photo is referenced as
            # `../sorFiles/photos/X.jpg`. The detail URL lives under
            # `/sor_id/`, so `../` resolves to the host root — the real
            # file is at `/sorFiles/photos/X.jpg`, NOT
            # `/sor_id/sorFiles/photos/X.jpg`.
            url = f"https://apps.isp.idaho.gov/{url[3:]}"
        elif not url.startswith("http"):
            url = f"{BASE}/{url}"
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


# ---------------------------------------------------------------------------
# HTML parsing helpers


def _parse_results(html: str, default_zip: str) -> list[_Hit]:
    """Pull (kno, internal_id, name, address, ...) tuples out of the
    main results table. Idaho's markup tags each cell with an id like
    `kno_field`, which keeps this resilient to row shuffles."""
    s = BeautifulSoup(html, "html.parser")
    hits: list[_Hit] = []
    # The main data table is the one with <td id="kno_field"> rows.
    for tr in s.find_all("tr"):
        kno_cell = tr.find(attrs={"id": "kno_field"})
        if not kno_cell:
            continue
        kno = kno_cell.get_text(strip=True)
        if not kno:
            continue
        name_cell = tr.find(attrs={"id": "nam_field"})
        name = name_cell.get_text(" ", strip=True) if name_cell else ""
        # Detail link inside name cell or image cell
        # Internal id is usually numeric but some records (e.g. "pending"
        # status) use an alphanumeric form like `P10054`. Match both.
        detail_a = (name_cell.find("a") if name_cell else None) or tr.find(
            "a", href=re.compile(r"SOR\?id=[A-Za-z0-9]+")
        )
        internal_id = ""
        if detail_a and detail_a.get("href"):
            m = re.search(r"id=([A-Za-z0-9]+)", detail_a["href"])
            if m:
                internal_id = m.group(1)
        adr_cell = tr.find(attrs={"id": "adr_field"})
        # Multiple cells share id="cty_field" — first is city, second is county
        cty_cells = tr.find_all(attrs={"id": "cty_field"})
        zip_cell = tr.find(attrs={"id": "zip_field"})
        sts_cell = tr.find(attrs={"id": "sts_field"})
        hits.append(
            _Hit(
                kno=kno,
                internal_id=internal_id,
                name=name,
                address=adr_cell.get_text(" ", strip=True) if adr_cell else "",
                city=cty_cells[0].get_text(strip=True) if cty_cells else "",
                county=cty_cells[1].get_text(strip=True) if len(cty_cells) > 1 else "",
                zip_code=zip_cell.get_text(strip=True) if zip_cell else default_zip,
                status=sts_cell.get_text(" ", strip=True) if sts_cell else "",
            )
        )
    return hits


def _has_next_page(html: str, current_page: int) -> bool:
    return re.search(rf'page={current_page + 1}\b', html) is not None


def _parse_detail(html: str, hit: _Hit) -> dict:
    s = BeautifulSoup(html, "html.parser")
    # The detail page uses a label-table pattern: alternating cells of
    # "Label:" and value. Walk every <tr>; for each TD whose text ends in
    # ":" treat the next TD as that field's value. Idaho's labels include
    # non-breaking spaces (e.g. "Birth\xa0Date:") so we normalize before
    # using the label as a key.
    fields: dict[str, str] = {}
    for tr in s.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        i = 0
        while i < len(cells) - 1:
            label = _norm_label(cells[i].get_text(" ", strip=True).rstrip(":"))
            value = cells[i + 1].get_text(" ", strip=True).replace("\xa0", " ")
            if label and value and label not in fields and len(label) < 60:
                fields[label] = value
            i += 2

    # Photo: full-size <img> referring to /sorFiles/photos/...
    photo_url = ""
    for img in s.find_all("img"):
        src = (img.get("src") or "").strip()
        if "sorFiles/photos/" in src and "/thumbs/" not in src:
            photo_url = src
            break

    # Offenses: parse rows that look like "<STATUTE> | <DESCRIPTION> | <DATE> | <JURISDICTION>"
    offenses: list[dict] = []
    seen_off = set()
    for tr in s.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        # Match the four-field offense pattern: short statute, long description,
        # long date string, short jurisdiction code.
        if len(cells) >= 4 and _looks_like_statute(cells[0]) and _looks_like_long_date(cells[2]):
            sig = (cells[0], cells[1], cells[2])
            if sig in seen_off:
                continue
            seen_off.add(sig)
            offenses.append(
                {
                    "statute": cells[0],
                    "description": cells[1],
                    "date": cells[2],
                    "jurisdiction": cells[3],
                }
            )

    aliases_text = fields.get("aliases", "")
    aliases = _parse_aliases(aliases_text)

    return {
        "kno": hit.kno,
        "internal_id": hit.internal_id,
        "reg_id": fields.get("reg id", ""),
        "name": fields.get("name", hit.name),
        "aliases": aliases,
        "birth_date": fields.get("birth date", ""),
        "birth_place": fields.get("birth place", ""),
        "race": fields.get("race", ""),
        "sex": fields.get("sex", ""),
        "height": fields.get("height", ""),
        "weight": fields.get("weight", ""),
        "eye_color": fields.get("eye color", ""),
        "hair_color": fields.get("hair color", ""),
        "last_photo_date": fields.get("last photo date", ""),
        "last_registered": fields.get("last registered", ""),
        "last_process": fields.get("last process", ""),
        "last_process_update": fields.get("last process update", ""),
        "last_verification_received": fields.get("last verification received", ""),
        "status": fields.get("reg status", hit.status),
        "address": hit.address,
        "city": hit.city,
        "county": hit.county,
        "zip_code": hit.zip_code,
        "offenses": offenses,
        "photo_url": photo_url,
    }


def _hit_only_payload(hit: _Hit) -> dict:
    """When detail fetch fails, ship just the search-row data."""
    return {
        "kno": hit.kno,
        "internal_id": hit.internal_id,
        "name": hit.name,
        "address": hit.address,
        "city": hit.city,
        "county": hit.county,
        "zip_code": hit.zip_code,
        "status": hit.status,
        "aliases": [],
        "offenses": [],
        "photo_url": "",
    }


def _flip_lastfirst(name: str) -> str:
    """`LAST, FIRST MIDDLE` -> `FIRST MIDDLE LAST`. Idaho's pages use the
    comma-form; the rest of our schema uses the natural-language form."""
    if "," in name:
        last, rest = name.split(",", 1)
        return f"{rest.strip()} {last.strip()}".strip()
    return name.strip()


def _norm_label(label: str) -> str:
    """nbsp -> space, collapse, lowercase. Idaho's labels carry `\\xa0`
    inside multi-word names."""
    return re.sub(r"\s+", " ", label.replace("\xa0", " ")).strip().lower()


def _parse_aliases(text: str) -> list[str]:
    """Aliases are emitted as a packed run of `LAST, FIRST MIDDLE`
    entries separated only by a space between the prior MIDDLE and the
    next LAST — e.g. `AMBRIZ, DANIEL MEJIA AMBRIZ, DANNY MEJIA MEJIA,
    DANNY`. Split before any `WORD,` (uppercase token then comma) to
    recover each `LAST, REST` chunk, flip to natural order, dedup
    case-insensitively."""
    if not text:
        return []
    t = text.replace("\xa0", " ").strip()
    if not t:
        return []
    # Insert a sentinel before each LASTNAME-followed-by-comma boundary
    # (skip the very first run-start to avoid an empty leading split).
    chunks = re.split(r"(?<=\s)(?=[A-Z][A-Z\-']+,)", t)
    out: list[str] = []
    seen = set()
    for p in chunks:
        p = p.strip().rstrip(",;")
        if not p:
            continue
        flipped = _flip_lastfirst(p)
        k = re.sub(r"\s+", " ", flipped).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(flipped)
    return out


def _looks_like_statute(s: str) -> bool:
    return bool(re.match(r"^\d{1,3}-\d{3,5}", s.strip()))


def _looks_like_long_date(s: str) -> bool:
    return bool(re.match(
        r"^(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},\s*\d{4}$",
        s.strip(),
    ))


def _parse_long_date(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.strptime(s, "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _sex(value: object) -> str:
    v = str(value or "").strip().upper()
    if v in {"M", "MALE"}:
        return "M"
    if v in {"F", "FEMALE"}:
        return "F"
    if v in {"X", "U"}:
        return "X"
    return "unknown"


def _height_to_cm(value: object) -> float | None:
    """Idaho writes height as `5 ft 07 in`. Parse and convert to cm."""
    if not value:
        return None
    m = re.match(r"(\d+)\s*ft\s*(\d+)\s*in", str(value).strip(), re.I)
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


def build() -> IdahoAdapter:
    return IdahoAdapter()
