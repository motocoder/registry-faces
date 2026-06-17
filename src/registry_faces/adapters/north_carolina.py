"""North Carolina state registry adapter — direct ASP.NET WebForms scrape.

Source: https://sexoffender.ncsbi.gov

NC SBI publishes the registry as an ASP.NET WebForms application gated
by a one-time "Acceptable Use Policy" click-through. No captcha. The
flow is:

  1. GET / — pull `__VIEWSTATE` + `__VIEWSTATEGENERATOR`
  2. POST / with those + `agree=Agree` — server sets session cookie
  3. POST `search.aspx` with last-name prefix → GridView results
     (50 records/page, paged via `__doPostBack(DaGrid$_ctl1$_ctlN, '')`)
  4. GET `details.aspx?srn=<SRN>` for full per-person schema
  5. GET `photo.aspx?srn=<SRN>` for mugshot

Two-phase ingest:

  Phase 1 (enumerate): sweep 4-letter last-name prefixes. NC's `lname`
    search requires `len(lname) >= 4` for true prefix matching — 1-3
    character searches return only exact matches. The default strategy
    is `top10k`: ~5K 4-letter prefixes derived from the top 10,000 US
    Census surnames (covers ~95% of NC registrants in ~30 minutes).
    `extended` adds census ranks 10K-50K (~99% coverage in ~90 min).
    `brute` sweeps the full AAAA..ZZZZ space (~457K prefixes, ~51 hr,
    100% coverage). Set via `REGISTRY_FACES_NC_SWEEP=brute`.

  Phase 2 (hydrate): fetch `details.aspx?srn=<SRN>` for each unique
    SRN and parse the labeled-field layout.

Schema notes:
  * `source_id` is the NC-assigned SRN (e.g. `018352S10`).
  * Aliases come from the "Alias Names:" labeled field.
  * Photos live at `photo.aspx?srn=<SRN>`; some records have alternate
    angles via `&o=L` / `&o=R` — we ship only the primary.
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
from ._nc_surnames import EXTENDED_LASTNAME_PREFIXES, TOP10K_LASTNAME_PREFIXES
from .base import Adapter

BASE = "https://sexoffender.ncsbi.gov"
HOME_URL = f"{BASE}/"
SEARCH_URL = f"{BASE}/search.aspx"
RESULTS_URL = f"{BASE}/results.aspx"
DETAILS_URL = f"{BASE}/details.aspx"
PHOTO_URL_TEMPLATE = f"{BASE}/photo.aspx?srn={{srn}}"
LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _build_brute_force_prefixes() -> tuple[str, ...]:
    return tuple(a + b + c + d for a in LETTERS for b in LETTERS for c in LETTERS for d in LETTERS)


# Three named sweep strategies. The default is "top10k" — fast (~30 min)
# and covers ~95% of NC records. "extended" adds Census ranks 10K-50K
# for ~99% coverage at ~3x the time. "brute" is the full
# AAAA..ZZZZ space (51 hours wall-time but guarantees completeness).
SWEEP_STRATEGIES: dict[str, tuple[str, ...]] = {
    "top10k": TOP10K_LASTNAME_PREFIXES,
    "extended": TOP10K_LASTNAME_PREFIXES + EXTENDED_LASTNAME_PREFIXES,
}

# Detail page label patterns we care about.
_LABEL_FIELDS = (
    "Race", "Sex", "Height", "Weight", "Hair", "Eyes", "Birth Date",
    "Registration Type", "Registering Sheriff", "Minimum Registration Period",
    "Alias Names", "Scars, Marks, Tattoos", "Last Address Verified",
    "Registration Status", "Registration Date", "Violations",
)


class NorthCarolinaAdapter(Adapter):
    jurisdiction = "US-NC"
    source_name = "North Carolina State Bureau of Investigation"

    def __init__(
        self,
        request_timeout: float = 60.0,
        request_delay_s: float = 0.2,
        retry_attempts: int = 3,
        retry_backoff: float = 2.0,
        progress_every: int = 250,
        sweep_strategy: str = "top10k",
        sweep_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        """Args:
            sweep_strategy: One of "top10k" (~5K prefixes, ~30 min, 95%
                coverage), "extended" (~15K prefixes, ~90 min, 99%
                coverage), or "brute" (~457K prefixes, ~51 hr, 100%).
                Ignored if `sweep_prefixes` is supplied.
            sweep_prefixes: Override the strategy with an explicit list
                of 4-letter prefixes. Lower-cased internally.
        """
        self.request_timeout = request_timeout
        self.request_delay_s = request_delay_s
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self.progress_every = progress_every
        if sweep_prefixes is not None:
            self.sweep_prefixes = tuple(p.lower() for p in sweep_prefixes)
        elif sweep_strategy == "brute":
            self.sweep_prefixes = _build_brute_force_prefixes()
        elif sweep_strategy in SWEEP_STRATEGIES:
            self.sweep_prefixes = tuple(p.lower() for p in SWEEP_STRATEGIES[sweep_strategy])
        else:
            raise ValueError(
                f"unknown sweep_strategy {sweep_strategy!r}; "
                f"expected one of {list(SWEEP_STRATEGIES) + ['brute']}"
            )
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
            self._accept_aup()
            seen: set[str] = set()
            print(
                f"  NC: enumerating across {len(self.sweep_prefixes)} "
                f"4-letter last-name prefixes",
                flush=True,
            )
            for i, prefix in enumerate(self.sweep_prefixes, start=1):
                for srn in self._enumerate_prefix(prefix):
                    seen.add(srn)
                if i % 250 == 0:
                    print(
                        f"    NC prefix={prefix.upper()}  "
                        f"done={i}/{len(self.sweep_prefixes)}  "
                        f"unique_so_far={len(seen)}",
                        flush=True,
                    )

            print(
                f"  NC: enumeration done. {len(seen)} unique SRNs. "
                f"Hydrating detail pages.",
                flush=True,
            )
            hydrated = 0
            for srn in sorted(seen):
                payload = self._fetch_detail(srn)
                if payload is None:
                    continue
                hydrated += 1
                if hydrated % self.progress_every == 0:
                    print(f"    NC hydrated={hydrated}/{len(seen)}", flush=True)
                yield payload
        finally:
            self._client.close()
            self._client = None

    def _accept_aup(self) -> None:
        assert self._client is not None
        html = self._get(HOME_URL)
        if html is None:
            raise RuntimeError("NC AUP page unreachable")
        vs, vsg, _ev = _parse_aspnet_state(html)
        self._post(
            HOME_URL,
            data={
                "__VIEWSTATE": vs,
                "__VIEWSTATEGENERATOR": vsg,
                "agree": "Agree",
            },
        )

    def _enumerate_prefix(self, prefix: str) -> Iterator[str]:
        """Submit name search for prefix, paginate, yield SRNs."""
        assert self._client is not None
        # Initial search form
        search_html = self._get(SEARCH_URL)
        if search_html is None:
            return
        vs, vsg, ev = _parse_aspnet_state(search_html)
        data = {
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            "lname": prefix,
            "fname": "",
            "age": "",
            "zip": "",
            "city": "",
            "county": "-1",
            "status": "-1",
            "searchbutton1": "Search",
        }
        results_html = self._post(SEARCH_URL, data=data)
        if results_html is None:
            return
        for srn in _extract_srns(results_html):
            yield srn

        # Paginate via DaGrid postbacks. The grid renders page links
        # as `__doPostBack('DaGrid$_ctl1$_ctlN', '')` — N is the page
        # index. Walk until no further page link appears.
        seen_pages = {1}
        while True:
            page_targets = _next_page_targets(results_html, seen_pages)
            if not page_targets:
                return
            for target, page_num in page_targets:
                vs, vsg, ev = _parse_aspnet_state(results_html)
                next_data = {
                    "__EVENTTARGET": target,
                    "__EVENTARGUMENT": "",
                    "__VIEWSTATE": vs,
                    "__VIEWSTATEGENERATOR": vsg,
                    "__EVENTVALIDATION": ev,
                    "lname": prefix,
                    "fname": "",
                    "age": "",
                    "zip": "",
                    "city": "",
                    "county": "-1",
                    "status": "-1",
                }
                next_html = self._post(RESULTS_URL, data=next_data)
                if next_html is None:
                    return
                results_html = next_html
                for srn in _extract_srns(results_html):
                    yield srn
                seen_pages.add(page_num)
                break  # Only follow the first new target each round
            else:
                return

    def _fetch_detail(self, srn: str) -> dict | None:
        html = self._get(f"{DETAILS_URL}?srn={srn}")
        if html is None:
            return None
        return _parse_detail(srn, html)

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
        print(f"  NC GET fail {url}: {last_err}", flush=True)
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
        print(f"  NC POST fail {url}: {last_err}", flush=True)
        return None

    # ---- normalize ----------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        srn = (raw.get("srn") or "").strip()
        name_lastfirst = (raw.get("name") or "").strip()
        full_name = _flip_lastfirst(name_lastfirst) or "UNKNOWN"

        dob = _parse_us_date(raw.get("birth_date"))
        identity = Identity(
            full_name=full_name,
            aliases=_split_aliases(raw.get("alias_names")),
            dob=dob,
            year_of_birth=dob.year if dob else None,
            sex=_sex(raw.get("sex")),
            race=(raw.get("race") or "").strip() or None,
            height_cm=_height_to_cm(raw.get("height")),
            weight_kg=_weight_to_kg(raw.get("weight")),
            eye_color=(raw.get("eyes") or "").strip() or None,
            hair_color=(raw.get("hair") or "").strip() or None,
            description=(raw.get("scars_marks_tattoos") or "").strip() or None,
        )

        addresses: list[Address] = []
        addr = (raw.get("address") or "").strip()
        city = (raw.get("city") or "").strip()
        state = (raw.get("state") or "").strip() or "NC"
        zip_code = (raw.get("zip") or "").strip()
        if any((addr, city, zip_code)):
            addresses.append(
                Address(
                    type="home",
                    street=addr or None,
                    city=city or None,
                    state=state,
                    zip=zip_code or None,
                )
            )

        offenses: list[Offense] = []
        for o in raw.get("convictions") or []:
            desc = (o.get("description") or "").strip()
            statute = (o.get("statute") or "").strip()
            if not (desc or statute):
                continue
            offenses.append(
                Offense(
                    raw_description=desc or statute,
                    raw_code=statute or None,
                    statute=statute or None,
                    conviction_date=_parse_us_date(o.get("conviction_date")),
                    jurisdiction=(o.get("county_state") or "").strip() or None,
                )
            )

        status_raw = (raw.get("registration_status") or "").strip().lower()
        if "absconder" in status_raw or "absconding" in status_raw:
            status = "absconder"
        elif "deceased" in status_raw:
            status = "deceased"
        elif "incarcerated" in status_raw:
            status = "incarcerated"
        elif "registered" in status_raw or "active" in status_raw:
            status = "active"
        else:
            status = "active"
        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=(status == "absconder"),
            registered_since=_parse_us_date(raw.get("registration_date")),
        )

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=srn,
                source_url=BASE,
                info_url=f"{DETAILS_URL}?srn={srn}",
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        srn = (raw.get("srn") or "").strip()
        if not srn:
            return []
        return [
            PhotoRef(
                url=PHOTO_URL_TEMPLATE.format(srn=srn),
                source_type="registry",
                source_name=self.source_name,
            )
        ]


# ---------------------------------------------------------------------------
# HTML parsing helpers


def _parse_aspnet_state(html: str) -> tuple[str, str, str]:
    s = BeautifulSoup(html, "html.parser")
    vs = s.find("input", attrs={"name": "__VIEWSTATE"})
    vsg = s.find("input", attrs={"name": "__VIEWSTATEGENERATOR"})
    ev = s.find("input", attrs={"name": "__EVENTVALIDATION"})
    return (
        (vs.get("value") if vs else "") or "",
        (vsg.get("value") if vsg else "") or "",
        (ev.get("value") if ev else "") or "",
    )


def _extract_srns(html: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for srn in re.findall(r"srn=([A-Z0-9]+)", html):
        if srn not in seen:
            seen.add(srn)
            out.append(srn)
    return out


def _next_page_targets(html: str, already_done: set[int]) -> list[tuple[str, int]]:
    """Return [(postback_target, page_number)] for pages not yet visited.

    Pagination row renders like:
        __doPostBack('DaGrid$_ctl1$_ctl2', '')   page 2
        __doPostBack('DaGrid$_ctl1$_ctl3', '')   page 3
        ...
    The ctlN index corresponds to position in the page link row,
    not the absolute page number. To recover page numbers we read
    the anchor text adjacent to each postback call.
    """
    out: list[tuple[str, int]] = []
    seen_targets: set[str] = set()
    s = BeautifulSoup(html, "html.parser")
    for a in s.find_all("a"):
        href = a.get("href", "")
        text = (a.get_text() or "").strip()
        m = re.search(r"__doPostBack\('(DaGrid\$_ctl1\$_ctl\d+)'", href)
        if not m or not text.isdigit():
            continue
        target = m.group(1)
        page_num = int(text)
        if target in seen_targets or page_num in already_done:
            continue
        seen_targets.add(target)
        out.append((target, page_num))
    return out


def _parse_detail(srn: str, html: str) -> dict:
    s = BeautifulSoup(html, "html.parser")
    text = s.get_text("\n", strip=True)

    # The detail page is a flat ASP.NET ContentPlaceHolder with the
    # offender name at the top, then address, then SRN, then a key/value
    # listing of attributes. Examples (newline-separated):
    #   ASH,NATHANIEL J
    #   8 UTOPIA RD
    #   ASHEVILLE, NC 28805
    #   SRN: 018352S10
    #   Race: W
    #   Sex: M
    #   Height: 5' 08"
    #   ...
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Anchor the offender section by finding the "SRN:" label line.
    # The page renders the SRN label and value on separate lines, with
    # the offender block above looking like:
    #   "Sex Offender Details"
    #   "LASTNAME,FIRSTNAME M"
    #   "<street address>"
    #   "<city>, ST <zip>"
    #   "SRN:"
    #   "<srn>"
    name = ""
    address = ""
    city_csz = ""  # "City, ST ZIP"
    for i, ln in enumerate(lines):
        if ln != "SRN:":
            continue
        # Walk backwards collecting offender block lines.
        addr_lines: list[str] = []
        csz = ""
        j = i - 1
        while j >= 0:
            line = lines[j]
            if _is_section_boundary(line):
                break
            if re.match(r"^.+,\s*[A-Z]{2}\s+\d{5}", line):
                csz = line
                j -= 1
                continue
            addr_lines.insert(0, line)
            j -= 1
        if addr_lines:
            if "," in addr_lines[0] and not any(ch.isdigit() for ch in addr_lines[0]):
                name = addr_lines[0]
                address = " ".join(addr_lines[1:])
            else:
                address = " ".join(addr_lines)
        city_csz = csz
        break

    city = state = zip_code = ""
    if city_csz:
        m = re.match(r"^(.+),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", city_csz)
        if m:
            city = m.group(1).strip()
            state = m.group(2)
            zip_code = m.group(3)

    fields = _extract_labeled_fields(lines)

    convictions = _parse_convictions(lines)

    return {
        "srn": srn,
        "name": name or fields.get("primary_name_at_time_of_sentencing", ""),
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "race": fields.get("race", ""),
        "sex": fields.get("sex", ""),
        "height": fields.get("height", ""),
        "weight": fields.get("weight", ""),
        "hair": fields.get("hair", ""),
        "eyes": fields.get("eyes", ""),
        "birth_date": fields.get("birth_date", ""),
        "registration_type": fields.get("registration_type", ""),
        "registering_sheriff": fields.get("registering_sheriff", ""),
        "minimum_registration_period": fields.get("minimum_registration_period", ""),
        "alias_names": fields.get("alias_names", ""),
        "scars_marks_tattoos": fields.get("scars,_marks,_tattoos", ""),
        "last_address_verified": fields.get("last_address_verified", ""),
        "registration_status": fields.get("registration_status", ""),
        "registration_date": fields.get("registration_date", ""),
        "violations": fields.get("violations", ""),
        "convictions": convictions,
    }


def _extract_labeled_fields(lines: list[str]) -> dict[str, str]:
    """Walk the line-flattened detail page collecting `Label: Value` pairs.

    The page renders label and value on adjacent lines (e.g. line N is
    "Race:" and line N+1 is "W"), so we look up the next non-empty line
    when the value isn't inline."""
    fields: dict[str, str] = {}
    label_set = {f"{lbl}:" for lbl in _LABEL_FIELDS}
    for i, ln in enumerate(lines):
        for label in _LABEL_FIELDS:
            prefix = f"{label}:"
            if not ln.startswith(prefix):
                continue
            value = ln[len(prefix):].strip()
            if not value and i + 1 < len(lines):
                # Multi-line aliases / marks: collect contiguous value lines
                # until we hit another label.
                value_lines = []
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    if any(nxt.startswith(lbl) for lbl in label_set):
                        break
                    if nxt in ("Offender Information", "Conviction Information",
                               "Sex Offender Details", "Note"):
                        break
                    if re.match(r"^Offense\s+\d+\s*$", nxt):
                        break
                    value_lines.append(nxt)
                    j += 1
                    # Only one line for single-value fields
                    if label not in ("Alias Names", "Scars, Marks, Tattoos"):
                        break
                value = " ".join(value_lines).strip()
            key = label.lower().replace(" ", "_")
            if key not in fields:
                fields[key] = value
            break
    return fields


def _parse_convictions(lines: list[str]) -> list[dict]:
    """Each conviction block starts with `Offense N` and contains labeled
    sub-fields. We walk linearly and collect them by block."""
    out: list[dict] = []
    current: dict | None = None
    sub_labels = (
        "Offense Date", "County - State", "Conviction Date", "Release Date",
        "Probation", "Confinement", "Statute", "Description",
        "Out-of-State Statute", "Out-of-State Description",
        "Victim's Age", "Offender's Age",
    )
    sub_label_set = {f"{lbl}:" for lbl in sub_labels}
    for i, ln in enumerate(lines):
        m = re.match(r"^Offense\s+(\d+)\s*$", ln)
        if m:
            if current is not None:
                out.append(current)
            current = {"offense_number": int(m.group(1))}
            continue
        if current is None:
            continue
        for label in sub_labels:
            prefix = f"{label}:"
            if not ln.startswith(prefix):
                continue
            value = ln[len(prefix):].strip()
            if not value and i + 1 < len(lines):
                # Walk forward until another sub-label or section break.
                value_parts = []
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    if any(nxt.startswith(lbl) for lbl in sub_label_set):
                        break
                    if re.match(r"^Offense\s+\d+\s*$", nxt) or nxt == "Note":
                        break
                    value_parts.append(nxt)
                    j += 1
                    # Single-line for most fields; descriptions may wrap.
                    if label not in ("Description", "Out-of-State Description"):
                        break
                value = " ".join(value_parts).strip()
            key = label.lower().replace(" ", "_").replace("-", "_").replace("'", "")
            if key not in current:
                current[key] = value
            break
    if current is not None:
        out.append(current)
    return out


def _is_section_boundary(line: str) -> bool:
    return line in ("Sex Offender Details", "Offender Information", "Conviction Information")


def _flip_lastfirst(name: str) -> str:
    """`LAST,FIRST MIDDLE` → `FIRST MIDDLE LAST`."""
    if "," in name:
        last, rest = name.split(",", 1)
        return f"{rest.strip()} {last.strip()}".strip()
    return name.strip()


def _split_aliases(text: object) -> list[str]:
    """Aliases arrive as a space-packed run of `LAST,FIRST` chunks, e.g.
    `ASH,BOOGIE ASH,NATE`. Split before each uppercase last-name boundary
    (`WORD,`)."""
    if not text:
        return []
    t = str(text).strip()
    if not t:
        return []
    chunks = re.split(r"(?<=\s)(?=[A-Z][A-Z\-']+,)", t)
    out: list[str] = []
    seen: set[str] = set()
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


def _parse_us_date(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"):
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
    """NC writes height as `5' 08"`."""
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


def build() -> NorthCarolinaAdapter:
    """Build with strategy controlled by `REGISTRY_FACES_NC_SWEEP` env var.

    Set to `top10k` (default), `extended`, or `brute`. Brute force is
    discouraged unless you need 100% coverage and have ~51 hours.
    """
    import os
    strategy = os.environ.get("REGISTRY_FACES_NC_SWEEP", "top10k")
    return NorthCarolinaAdapter(sweep_strategy=strategy)
