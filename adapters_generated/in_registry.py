"""India (Haryana) — Haryana Police public Sex Offender register.

Source type: server-rendered, paginated ASP.NET WebForms GridView.

India's NATIONAL registry (NCRB's National Database on Sexual Offenders, NDSO)
is law-enforcement-only — access is restricted to agencies on the Inter-operable
Criminal Justice System (ICJS), so it does not qualify as a public source.

Haryana Police, however, exposes an enumerable, unauthenticated public listing
of registered sex offenders (no login, no CAPTCHA) at:

    https://haryanapolice.gov.in/Citizen Services/ViewSexOffenderinformationMenuLess

It is a classic ASP.NET GridView (`gdvSexOffenderInformation`) paged via
`__doPostBack(..., 'Page$N')`. Each row publishes: a per-page serial, a stable
offender number (`hdnSexOffenderNo`, used as `source_id`), the offender name
(Devanagari or Latin, verbatim), relative type + relative name, an age-group
band, and gender. No street address, no offence text, and no photo URL are
served on the listing surface.

Pagination requires round-tripping the WebForms tokens (`__VIEWSTATE`,
`__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`) refreshed from each response. A
GridView clamps an out-of-range page request to the last page and re-serves it,
so we stop when a page repeats the previous page's set of offender numbers.

Photos: the source serves no image URL on this listing, so `extract_photos`
returns an empty list per scope (only source-published image URLs are allowed).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from registry_faces.schema import (
    Address,
    Identity,
    OffenderRecord,
    Source,
)
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

LISTING = (
    "https://haryanapolice.gov.in/Citizen Services/"
    "ViewSexOffenderinformationMenuLess"
)
USER_AGENT = (
    "registry-faces/1.0 (+public Haryana Police sex-offender register index)"
)

GRID_ID = "ctl00$ContentPlaceHolder1$gdvSexOffenderInformation"
GRID_ID_RE = re.compile(r"gdvSexOffenderInformation$")

# Safety bound only: stop is driven by repeated/empty pages. This cap merely
# prevents a runaway loop if the GridView paging contract changes.
MAX_PAGES = 2000

_SEX = {"MALE": "M", "FEMALE": "F"}

# The WebForms hidden tokens that must be echoed back on every postback.
_TOKENS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


def _sex(value: str | None) -> str:
    return _SEX.get((value or "").strip().upper(), "unknown")


def _cell(row, suffix: str) -> str | None:
    """Text of the `<span id=...lbl{suffix}_N>` (or name link) cell, or None."""
    el = row.find(id=re.compile(rf"_{suffix}_\d+$"))
    if el is None:
        return None
    text = el.get_text(strip=True)
    return text or None


class InRegistryAdapter(Adapter):
    jurisdiction = "IN-HR"
    source_name = "Haryana Police Sex Offender Register (India)"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
            # The gov SSL chain is frequently incomplete; the listing is public
            # read-only data, so verification adds no integrity guarantee here.
            verify=False,
        )

    # -- fetch ----------------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        resp = self._client.get(LISTING)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        page = 1
        prev_ids: set[str] = set()
        while page <= MAX_PAGES:
            rows = list(self._parse_rows(soup, page))
            page_ids = {r["sex_offender_no"] for r in rows if r["sex_offender_no"]}
            # An out-of-range page is clamped to the last page and re-served;
            # an identical id-set means we have run off the end.
            if not rows or (page > 1 and page_ids == prev_ids):
                break
            yield from rows
            prev_ids = page_ids

            soup = self._post_page(soup, page + 1)
            if soup is None:
                break
            page += 1

    def _parse_rows(self, soup: BeautifulSoup, page: int) -> Iterator[dict]:
        table = soup.find("table", id=GRID_ID_RE)
        if table is None:
            return
        for tr in table.find_all("tr"):
            offender_no_el = tr.find(
                "input", attrs={"name": re.compile(r"hdnSexOffenderNo$")}
            )
            if offender_no_el is None:
                continue  # header / pager row
            yield {
                "sex_offender_no": (offender_no_el.get("value") or "").strip(),
                "serial": _cell(tr, "lblID"),
                "name": _cell(tr, "lnkName"),
                "relative_type": _cell(tr, "lblRelativeType"),
                "relative_name": _cell(tr, "lblRelativeName"),
                "age_group": _cell(tr, "lblAgeGroup"),
                "gender": _cell(tr, "lblGender"),
                "page": page,
                "source_url": LISTING,
            }

    def _post_page(self, soup: BeautifulSoup, page: int) -> BeautifulSoup | None:
        data = {
            "__EVENTTARGET": GRID_ID,
            "__EVENTARGUMENT": f"Page${page}",
        }
        for name in _TOKENS:
            el = soup.find("input", {"name": name})
            if el is None:
                return None
            data[name] = el.get("value", "")
        resp = self._client.post(LISTING, data=data)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")

    # -- normalize ------------------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)

        # The relative type/name is the only descriptive detail published.
        rel_type = raw.get("relative_type")
        rel_name = raw.get("relative_name")
        description = None
        if rel_name:
            description = f"{rel_type}: {rel_name}" if rel_type else rel_name

        identity = Identity(
            full_name=(raw.get("name") or "").strip(),
            sex=_sex(raw.get("gender")),
            description=description,
        )

        # No street/city is published — only the jurisdiction (Haryana, India).
        addresses = [Address(type="home", state="Haryana", country="IN")]

        return OffenderRecord(
            source=Source(
                jurisdiction="IN-HR",
                source_id=raw.get("sex_offender_no") or "",
                source_url=LISTING,
                info_url=LISTING,
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # The public listing serves no image URL, so there is nothing to return.
        return []


def build() -> InRegistryAdapter:
    return InRegistryAdapter()
