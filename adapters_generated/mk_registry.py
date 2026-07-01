"""North Macedonia — Register of Pedophiles (Регистар на педофили).

Source type: server-rendered ASP.NET WebForms search page backed by a
DevExpress ``ASPxGridView`` (paginated HTML via WebForms callbacks).

North Macedonia is one of the rare non-US jurisdictions that runs an OFFICIAL,
public, enumerable sex-offender registry. It is coordinated by the Ministry of
Labour and Social Policy (Министерство за труд и социјална политика) and kept by
the State institution Center for Social Activities (ЈУ Завод за социјални
дејности), under the 2012 law on a special registry of persons convicted by
final judgment for sexual abuse of minors / pedophilia:

    https://registarnapedofili.mk/Prebaruvanje.aspx

The public search page lets anyone browse the full list. Submitting the search
with all filters blank returns every record, paged 5 per page (≈361 records /
73 pages as of mid-2026). Each record publishes: given name, surname, nickname,
date of birth, municipality / settlement / street of residence, the conviction
text (statute article + sentence + release status), and a portrait photo.

Enumeration mechanics
---------------------
The page is plain ASP.NET WebForms + DevExpress, so there is no JSON API:

  1. GET the page to pick up ``__VIEWSTATE`` / ``__EVENTVALIDATION``.
  2. POST ``btnSearch`` with every filter blank -> page 0 of the grid as full
     HTML, plus the grid's ``grid$CallbackState`` blob.
  3. For every further page, POST a DevExpress grid "page-next" callback
     (``__CALLBACKID=grid``, ``__CALLBACKPARAM=c0:GB|20;12|PAGERONCLICK3|PBN;``).
     Each callback's "next" is relative to the ``grid$CallbackState`` blob sent
     with it, and the response carries an updated blob, so the state is *chained*
     page to page. (Absolute ``PN<n>`` jumps are unreliable — the pager block
     math makes e.g. ``PN10`` alias page 1 — so we step forward one page at a
     time, which is deterministic.)

Each row is a fixed template of ``<label class="dxeBase" id="grid_row{N}_...">``
cells, so fields are mapped by their stable per-row id suffix.

Photos
------
Each portrait is rendered by a DevExpress ``ASPxBinaryImage`` whose ``src`` is a
``/Prebaruvanje.aspx?DXCache=<guid>`` reference. That GUID points at a
*per-session, in-memory image cache* — it is not a stable, re-fetchable URL and
404s outside the originating session. Per scope, ``extract_photos`` only returns
durable, source-served image *URLs*; since the registry exposes no such URL the
method returns an empty list. The ephemeral cache reference is still preserved
in ``record.raw["photo_cache_url"]`` for a later same-session step. (Same
posture as the Kazakhstan adapter, whose portraits are base64-inlined.)
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from registry_faces.schema import (
    Address,
    Identity,
    Offense,
    OffenderRecord,
    Source,
)
from web_scrubber.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://registarnapedofili.mk"
SEARCH = f"{BASE}/Prebaruvanje.aspx"
USER_AGENT = (
    "registry-faces/1.0 (+public MK pedophile-registry index)"
)

# Defensive cap only: ~73 pages today. Paging also stops on the first empty
# page, so this just prevents a runaway loop if the pager text shape changes.
MAX_PAGES = 2000

# Hidden ASP.NET / DevExpress fields whose values must be echoed back verbatim.
_HIDDEN_IDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")

# Per-row label id suffix -> logical field name. The suffixes are auto-assigned
# by the WebForms template and are identical for every data row.
_FIELD_BY_SUFFIX = {
    "lblFirstName": "first_name",
    "ASPxLabel3": "last_name",
    "lblTitle": "nickname",
    "lblBirthDate": "birth_date",
    "ASPxLabel4": "municipality",
    "lblHireDate": "settlement",
    "lblNotes": "street",
    "ASPxLabel5": "offense",
}

_LABEL_RE = re.compile(
    r"""<label[^>]*\bid=["']grid_row(\d+)_(\w+)["'][^>]*>(.*?)</label>""",
    re.DOTALL,
)
_PHOTO_RE = re.compile(
    r"""id=["']grid_row(\d+)_ASPxBinaryImage1["'][^>]*?"""
    r"""src=["'](/Prebaruvanje\.aspx\?DXCache=[^"'\\]+)""",
    re.DOTALL,
)
_TOTAL_RE = re.compile(r"of\s+(\d+)\s*\(\s*(\d+)\s*items\)")
_STATUTE_RE = re.compile(r"чл\.?\s*([0-9]+(?:-[А-Яа-яЃѓЌќЅѕЏџЉљЊњ])?[а-я]?)")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text or None


def _parse_dob(value: str | None) -> datetime | None:
    if not value:
        return None
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
    if not m:
        return None
    day, month, year = (int(p) for p in m.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


class MkRegistryAdapter(Adapter):
    jurisdiction = "MK"
    source_name = (
        "North Macedonia Register of Pedophiles (Регистар на педофили) — "
        "Ministry of Labour and Social Policy"
    )

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=60.0,
            follow_redirects=True,
        )

    # -- fetch ----------------------------------------------------------------

    @staticmethod
    def _hidden(text: str, field_id: str) -> str:
        m = re.search(
            r'id="' + re.escape(field_id) + r'"[^>]*value="([^"]*)"', text
        )
        return m.group(1) if m else ""

    @staticmethod
    def _named(text: str, field_name: str) -> str:
        m = re.search(
            r'name="' + re.escape(field_name) + r'"[^>]*value="([^"]*)"', text
        )
        return m.group(1) if m else ""

    @staticmethod
    def _unescape_callback(text: str) -> str:
        # DevExpress callback responses ship HTML with JS-escaped quotes/slashes.
        return text.replace("\\u0027", "'").replace('\\u0022', '"').replace("\\/", "/")

    def _blank_filters(self) -> dict:
        return {
            "tbIme": "", "tbPrezime": "", "tbPrekar": "",
            "cmbOpstinaZiveenje_VI": "", "cmbOpstinaZiveenje": "",
            "cmbMestoZiveenje_VI": "", "cmbMestoZiveenje": "",
            "cmbUlicaZiveenje_VI": "", "cmbUlicaZiveenje": "",
        }

    def fetch(self) -> Iterator[dict]:
        landing = self._client.get(SEARCH)
        landing.raise_for_status()
        h0 = landing.text

        # Step 2: submit the search with every filter blank -> page 0.
        search_data = {hid: self._hidden(h0, hid) for hid in _HIDDEN_IDS}
        search_data.update(self._blank_filters())
        search_data["btnSearch"] = "Пребарај"
        resp = self._client.post(SEARCH, data=search_data)
        resp.raise_for_status()
        page0 = resp.text

        total_pages = self._total_pages(page0)
        base_cb = {hid: self._hidden(page0, hid) for hid in _HIDDEN_IDS}
        base_cb["__CALLBACKID"] = "grid"
        base_cb["__CALLBACKPARAM"] = "c0:GB|20;12|PAGERONCLICK3|PBN;"
        base_cb.update(self._blank_filters())

        seen: set[str] = set()
        # Page 0 is already in hand as full HTML.
        yield from self._emit_rows(page0, 0, seen)

        # Step 3: walk the remaining pages one "page-next" callback at a time,
        # chaining the grid's callback-state blob from each response into the
        # next request.
        callback_state = self._named(page0, "grid$CallbackState")
        for page_no in range(1, min(total_pages, MAX_PAGES)):
            cb = dict(base_cb)
            cb["grid$CallbackState"] = callback_state
            r = self._client.post(SEARCH, data=cb)
            r.raise_for_status()
            body = self._unescape_callback(r.text)
            emitted = list(self._emit_rows(body, page_no, seen))
            if not emitted:
                break
            yield from emitted
            next_state = self._named(body, "grid$CallbackState")
            if not next_state:
                break
            callback_state = next_state

    def _total_pages(self, text: str) -> int:
        m = _TOTAL_RE.search(text)
        if not m:
            return 1
        total_items = int(m.group(2))
        # Grid shows 5 records per page.
        return max(1, (total_items + 4) // 5)

    def _emit_rows(self, text: str, page_no: int, seen: set) -> Iterator[dict]:
        rows: dict[int, dict] = {}
        for ridx, suffix, value in _LABEL_RE.findall(text):
            field = _FIELD_BY_SUFFIX.get(suffix)
            if field is None:
                continue
            rows.setdefault(int(ridx), {})[field] = _clean(value)
        photos = {int(r): u for r, u in _PHOTO_RE.findall(text)}

        for ridx in sorted(rows):
            raw = rows[ridx]
            # Drop the all-empty placeholder rows DevExpress sometimes renders.
            if not (raw.get("first_name") or raw.get("last_name")):
                continue
            if photos.get(ridx):
                raw["photo_cache_url"] = BASE + photos[ridx]
            raw["page"] = page_no
            key = self._source_id(raw)
            if key in seen:
                continue
            seen.add(key)
            raw["source_id"] = key
            yield raw

    @staticmethod
    def _source_id(raw: dict) -> str:
        parts = "|".join(
            (raw.get(f) or "")
            for f in (
                "first_name", "last_name", "nickname",
                "birth_date", "municipality", "settlement", "street",
            )
        )
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()

    # -- normalize ------------------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)

        full_name = " ".join(
            p for p in (raw.get("first_name"), raw.get("last_name")) if p
        )
        aliases = [raw["nickname"]] if raw.get("nickname") else []
        dob = _parse_dob(raw.get("birth_date"))

        identity = Identity(
            full_name=full_name or "(unknown)",
            aliases=aliases,
            dob=dob,
            year_of_birth=dob.year if dob else None,
        )

        addresses: list[Address] = []
        if any(raw.get(f) for f in ("street", "settlement", "municipality")):
            addresses.append(
                Address(
                    type="home",
                    street=raw.get("street"),
                    city=raw.get("settlement") or raw.get("municipality"),
                    state=raw.get("municipality"),
                    country="MK",
                )
            )

        offenses: list[Offense] = []
        offense_text = raw.get("offense")
        if offense_text:
            statute = _STATUTE_RE.search(offense_text)
            offenses.append(
                Offense(
                    raw_description=offense_text,
                    statute=statute.group(0) if statute else None,
                    jurisdiction="MK",
                )
            )

        return OffenderRecord(
            source=Source(
                jurisdiction="MK",
                source_id=raw["source_id"],
                source_url=SEARCH,
                info_url=SEARCH,
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # Portraits exist but are only reachable through a per-session DXCache
        # reference (see module docstring), not a durable URL. Nothing
        # re-fetchable to return.
        return []


def build() -> MkRegistryAdapter:
    return MkRegistryAdapter()
