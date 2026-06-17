"""North Dakota state registry adapter — direct ASP.NET MVC scrape.

Source: https://sexoffender.nd.gov

ND AG runs the registry as a plain ASP.NET MVC application with NO
captcha and NO acceptance gate. The DataTables-backed search at
`/offender/name-search` returns up to ~1.5K offenders per request
(all on one HTML page — no pagination). Names are matched as
*substring*, not prefix, so a single-letter sweep over A..P is
sufficient to enumerate all unique offenders (the substring "P"
captures everyone whose last name contains a P, after which no new
IDs appear).

Two-phase ingest:

  Phase 1 (enumerate): POST `/offender/name-search` with each letter
    as `Search.LastName` and an empty FirstName. Collect detail-page
    GUIDs from the response. Deduplicate.

  Phase 2 (hydrate): GET `/offender/details/<guid>` for each unique
    GUID and parse the visible labels.

Schema notes:
  * `source_id` is the ND-assigned offender GUID.
  * The detail page renders labels and values on adjacent lines, so
    we parse by walking the line sequence and pairing `Label:` with
    the next non-label line.
  * ND publishes year of birth only (not full DOB).
  * Risk levels: HIGH / MODERATE / LOW / UNDETERMINED. Status:
    REGISTERED / INCARCERATED / NON-COMPLIANT / WHEREABOUTS
    UNKNOWN / DECEASED. We map these to canonical statuses.
  * Photo at `https://sexoffender.nd.gov/photos/<GUID>.JPG` is the
    primary mugshot.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from ..photos import PhotoRef
from ..schema import Address, Identity, Offense, OffenderRecord, Registration, Source
from .base import Adapter

BASE = "https://sexoffender.nd.gov"
SEARCH_URL = f"{BASE}/offender/search"
NAME_SEARCH_URL = f"{BASE}/offender/name-search"
DETAIL_URL_TEMPLATE = f"{BASE}/offender/details/{{guid}}"
PHOTO_URL_TEMPLATE = f"{BASE}/photos/{{guid_upper}}.JPG"
LETTERS = "abcdefghijklmnopqrstuvwxyz"

GUID_RE = re.compile(
    r"/offender/details/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

_LABEL_FIELDS = (
    "Name", "Aliases", "Birthdate", "Sex", "Race", "Height", "Weight",
    "Eye Color", "Hair Color", "Skin", "Registration Expiration",
    "Risk Level", "Registration Status", "Ethnicity",
)

_STATUS_MAP = {
    "REGISTERED": "active",
    "ACTIVE": "active",
    "INCARCERATED": "incarcerated",
    "NON-COMPLIANT": "absconder",
    "NONCOMPLIANT": "absconder",
    "WHEREABOUTS UNKNOWN": "absconder",
    "ABSCONDER": "absconder",
    "DECEASED": "deceased",
    "EXPIRED": "removed",
}


class NorthDakotaAdapter(Adapter):
    jurisdiction = "US-ND"
    source_name = "North Dakota Attorney General"

    def __init__(
        self,
        request_timeout: float = 60.0,
        request_delay_s: float = 0.2,
        retry_attempts: int = 3,
        retry_backoff: float = 2.0,
        progress_every: int = 200,
    ) -> None:
        self.request_timeout = request_timeout
        self.request_delay_s = request_delay_s
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self.progress_every = progress_every
        self._fetched_at: datetime | None = None
        self._client: httpx.Client | None = None
        self._token: str = ""

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
            self._refresh_token()
            seen: set[str] = set()
            print(
                f"  ND: enumerating across {len(LETTERS)} substring letters",
                flush=True,
            )
            empty_streak = 0
            for letter in LETTERS:
                ids = self._enumerate_letter(letter)
                new = [g for g in ids if g not in seen]
                seen.update(ids)
                print(
                    f"    ND letter={letter.upper()}  found={len(ids)}  "
                    f"new={len(new)}  total={len(seen)}",
                    flush=True,
                )
                # Cut the sweep short if 4 letters in a row add nothing
                # — substring matching has already covered the population.
                if not new:
                    empty_streak += 1
                    if empty_streak >= 4:
                        print(
                            f"  ND: 4 letters with no new IDs; ending enumeration early",
                            flush=True,
                        )
                        break
                else:
                    empty_streak = 0

            print(
                f"  ND: enumeration done. {len(seen)} unique offenders. "
                f"Hydrating detail pages.",
                flush=True,
            )
            hydrated = 0
            for guid in sorted(seen):
                payload = self._fetch_detail(guid)
                if payload is None:
                    continue
                hydrated += 1
                if hydrated % self.progress_every == 0:
                    print(f"    ND hydrated={hydrated}/{len(seen)}", flush=True)
                yield payload
        finally:
            self._client.close()
            self._client = None

    def _refresh_token(self) -> None:
        html = self._get(SEARCH_URL)
        if html is None:
            raise RuntimeError("ND search page unreachable")
        s = BeautifulSoup(html, "html.parser")
        inp = s.find("input", attrs={"name": "__RequestVerificationToken"})
        self._token = (inp.get("value") if inp else "") or ""

    def _enumerate_letter(self, letter: str) -> list[str]:
        html = self._post(
            NAME_SEARCH_URL,
            data={
                "Search.FirstName": "",
                "Search.LastName": letter,
                "__RequestVerificationToken": self._token,
            },
        )
        if html is None:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for guid in GUID_RE.findall(html):
            if guid not in seen:
                seen.add(guid)
                out.append(guid)
        return out

    def _fetch_detail(self, guid: str) -> dict | None:
        html = self._get(DETAIL_URL_TEMPLATE.format(guid=guid))
        if html is None:
            return None
        return _parse_detail(guid, html)

    # ---- HTTP helpers -------------------------------------------------

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
                time.sleep(self.retry_backoff * (attempt + 1))
        print(f"  ND GET fail {url}: {last_err}", flush=True)
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
                time.sleep(self.retry_backoff * (attempt + 1))
        print(f"  ND POST fail {url}: {last_err}", flush=True)
        return None

    # ---- normalize ----------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        guid = (raw.get("guid") or "").strip()
        name_lastfirst = (raw.get("name") or "").strip()
        full_name = _flip_lastfirst(name_lastfirst) or "UNKNOWN"

        yob = _parse_year(raw.get("birthdate"))

        identity = Identity(
            full_name=full_name,
            aliases=_split_aliases(raw.get("aliases")),
            year_of_birth=yob,
            sex=_sex(raw.get("sex")),
            race=(raw.get("race") or "").strip() or None,
            height_cm=_height_to_cm(raw.get("height")),
            weight_kg=_weight_to_kg(raw.get("weight")),
            eye_color=(raw.get("eye_color") or "").strip() or None,
            hair_color=(raw.get("hair_color") or "").strip() or None,
        )

        addresses: list[Address] = []
        for addr in raw.get("addresses") or []:
            a = _to_address(addr)
            if a is not None:
                addresses.append(a)

        offenses: list[Offense] = []
        for o in raw.get("offenses") or []:
            desc = (o.get("offense") or "").strip()
            if not desc:
                continue
            offenses.append(
                Offense(
                    raw_description=desc,
                    raw_code=(o.get("statute") or "").strip() or None,
                    statute=(o.get("statute") or "").strip() or None,
                    conviction_date=_parse_us_date(o.get("conviction_date")),
                    jurisdiction=(o.get("jurisdiction") or "").strip() or None,
                )
            )

        status_raw = (raw.get("registration_status") or "").strip().upper()
        status = _STATUS_MAP.get(status_raw, "active" if status_raw else "active")
        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=(status == "absconder"),
        )

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=guid,
                source_url=BASE,
                info_url=DETAIL_URL_TEMPLATE.format(guid=guid),
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        guid = (raw.get("guid") or "").strip()
        if not guid:
            return []
        # ND serves photos at uppercase-GUID.JPG. If the detail page
        # had a specific src we'd use that, but the canonical pattern
        # is reliable.
        url = (raw.get("photo_url") or "").strip()
        if not url:
            url = PHOTO_URL_TEMPLATE.format(guid_upper=guid.upper())
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


# ---------------------------------------------------------------------------
# Detail page parsing


def _parse_detail(guid: str, html: str) -> dict:
    s = BeautifulSoup(html, "html.parser")
    text = s.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    fields = _extract_label_value_pairs(lines, _LABEL_FIELDS)

    # Height is two-line: "Height:" then "5'" then "8\"". Combine.
    height = _extract_height(lines)
    if height:
        fields["height"] = height

    addresses = _parse_addresses(lines)
    offenses = _parse_offenses(lines)

    # Photo URL — prefer the explicit src in the page when present.
    photo_url = ""
    for img in s.find_all("img"):
        src = (img.get("src") or "").strip()
        if "/photos/" in src and src.lower().endswith(".jpg"):
            photo_url = src if src.startswith("http") else BASE + src
            break

    return {
        "guid": guid,
        "name": fields.get("name", ""),
        "aliases": fields.get("aliases", ""),
        "birthdate": fields.get("birthdate", ""),
        "sex": fields.get("sex", ""),
        "race": fields.get("race", ""),
        "height": fields.get("height", ""),
        "weight": fields.get("weight", ""),
        "eye_color": fields.get("eye_color", ""),
        "hair_color": fields.get("hair_color", ""),
        "skin": fields.get("skin", ""),
        "registration_expiration": fields.get("registration_expiration", ""),
        "risk_level": fields.get("risk_level", ""),
        "registration_status": fields.get("registration_status", ""),
        "ethnicity": fields.get("ethnicity", ""),
        "addresses": addresses,
        "offenses": offenses,
        "photo_url": photo_url,
    }


def _extract_label_value_pairs(
    lines: list[str], labels: tuple[str, ...]
) -> dict[str, str]:
    """Walk the line list collecting `Label:` followed by the next
    non-label line as the value. ND wraps both label and value as
    standalone text nodes."""
    fields: dict[str, str] = {}
    label_lookups = {f"{lbl}:": lbl.lower().replace(" ", "_") for lbl in labels}
    label_strs = set(label_lookups.keys())
    for i, ln in enumerate(lines):
        if ln in label_lookups:
            key = label_lookups[ln]
            if key in fields:
                continue
            # Next non-label, non-empty line is the value.
            for j in range(i + 1, min(len(lines), i + 5)):
                nxt = lines[j]
                if nxt in label_strs:
                    break
                if nxt.endswith(":"):
                    break
                fields[key] = nxt
                break
    return fields


def _extract_height(lines: list[str]) -> str | None:
    """Height renders as three lines: `Height:`, `5'`, `8\"`."""
    for i, ln in enumerate(lines):
        if ln == "Height:" and i + 2 < len(lines):
            a, b = lines[i + 1], lines[i + 2]
            if re.match(r"^\d+'$", a) and re.match(r"^\d+\"?$", b):
                return f"{a} {b}"
    return None


def _parse_addresses(lines: list[str]) -> list[dict]:
    """ND renders residence addresses under `Residence Addresses` header
    as `street`, `city, ST, zip`, `county`. Multiple blocks separated by
    blank-ish boundaries; we read until the next section header."""
    out: list[dict] = []
    section_starts = {
        "Residence Addresses": "home",
        "Additional Addresses": "other",
        "Employer Addresses": "work",
        "School Addresses": "school",
    }
    section_ends = {
        "Address Information", "Vehicles", "Qualifying Offense Information",
        "Other State Offenses", "Show Map",
        *section_starts.keys(),
    }
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln in section_starts:
            addr_type = section_starts[ln]
            j = i + 1
            block_lines: list[str] = []
            while j < len(lines):
                nxt = lines[j]
                if nxt in section_ends:
                    break
                if nxt == "No information available":
                    break
                block_lines.append(nxt)
                j += 1
            # Parse each address from contiguous lines
            addr = None
            for line in block_lines:
                if line == "Show Map":
                    if addr is not None:
                        out.append(addr)
                        addr = None
                    continue
                csz = re.match(r"^(.+),\s*([A-Z]{2}),?\s+(\d{5})(?:-\d{4})?$", line)
                if csz:
                    if addr is None:
                        addr = {"type": addr_type}
                    addr["city"] = csz.group(1).strip()
                    addr["state"] = csz.group(2)
                    addr["zip"] = csz.group(3)
                    continue
                # County line is single-word ALL CAPS without comma
                if line.isupper() and "," not in line and not any(
                    ch.isdigit() for ch in line
                ):
                    if addr is not None:
                        addr["county"] = line
                    continue
                # Otherwise treat as street
                if addr is None:
                    addr = {"type": addr_type, "street": line}
                elif "street" not in addr:
                    addr["street"] = line
            if addr is not None:
                out.append(addr)
            i = j
            continue
        i += 1
    return out


def _parse_offenses(lines: list[str]) -> list[dict]:
    """Parse the Qualifying Offense Information section."""
    out: list[dict] = []
    in_section = False
    current: dict = {}
    label_map = {
        "Offense:": "offense",
        "Conviction Date:": "conviction_date",
        "Jurisdiction & State:": "jurisdiction",
        "Disposition:": "disposition",
    }
    for i, ln in enumerate(lines):
        if ln == "Qualifying Offense Information":
            in_section = True
            continue
        if not in_section:
            continue
        if ln in ("Other State Offenses", "Address Information", "Vehicles"):
            if current:
                out.append(current)
                current = {}
            break
        for label, key in label_map.items():
            if ln == label and i + 1 < len(lines):
                value = lines[i + 1]
                if key == "offense" and current:
                    out.append(current)
                    current = {}
                # Try to split "DESC; STATUTE" from the offense line
                if key == "offense":
                    parts = value.rsplit(";", 1)
                    if len(parts) == 2 and re.match(r"^\s*[\d.\-A-Z]+\s*$", parts[1]):
                        current["offense"] = parts[0].strip()
                        current["statute"] = parts[1].strip()
                    else:
                        current["offense"] = value.strip()
                else:
                    current[key] = value.strip()
                break
    if current:
        out.append(current)
    return out


# ---------------------------------------------------------------------------
# Field helpers


def _to_address(d: dict) -> Address | None:
    street = (d.get("street") or "").strip() or None
    city = (d.get("city") or "").strip() or None
    state = (d.get("state") or "").strip() or "ND"
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


def _flip_lastfirst(name: str) -> str:
    if "," in name:
        last, rest = name.split(",", 1)
        return f"{rest.strip()} {last.strip()}".strip()
    return name.strip()


def _split_aliases(value: object) -> list[str]:
    if not value:
        return []
    s = str(value).strip()
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[,;\n]", s) if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        flipped = _flip_lastfirst(p)
        k = flipped.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(flipped)
    return out


def _parse_year(value: object) -> int | None:
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{4})$", s)
    if m:
        return int(m.group(1))
    # Try full date forms in case ND adds them later.
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).year
        except ValueError:
            continue
    return None


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
    v = str(value or "").strip().upper()
    if v in {"M", "MALE"}:
        return "M"
    if v in {"F", "FEMALE"}:
        return "F"
    return "unknown"


def _height_to_cm(value: object) -> float | None:
    """ND writes height as `5' 8\"`."""
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
    m = re.match(r"(\d+)\s*LBS?", str(value).strip(), re.I)
    if not m:
        return None
    pounds = int(m.group(1))
    if pounds <= 0 or pounds > 1000:
        return None
    return round(pounds * 0.45359237, 1)


def build() -> NorthDakotaAdapter:
    return NorthDakotaAdapter()
