"""Montana DOJ sex offender registry adapter.

Source: https://app.doj.mt.gov/apps/svow/search/

Montana publishes the SVOR (Sexual or Violent Offender Registry) via
an ArcGIS Web App backed by a hosted FeatureServer. The web app is a
map viewer, but the FeatureServer underneath is a clean, paginated
JSON REST endpoint — no captcha, no auth, no disclaimer cookie.

  FeatureServer: https://services.arcgis.com/lnWnvGNQtyvDCFPs/.../Montana_SVOR_Web/FeatureServer/0
  maxRecordCount: 2000 per page
  Total rows: ~7K

The FeatureServer publishes one row per (PERS_SID, LOC_ID) — people
with multiple registered addresses get one row per location, all
sharing the same PERS_SID. The adapter pages through every row, then
dedups by PERS_SID and merges all addresses for each person into a
single OffenderRecord.

The map app links to `offender-details.aspx` for per-person profiles,
but every variant of that URL returns 404 — Montana doesn't publish a
detail page externally. Everything we can know is already in the
FeatureServer attributes:

  PERS_SID, NAME (LAST, FIRST MIDDLE), OFF_TYP, TIER_LVL, DESIGNATION,
  NONCOMPLIANT (Y or null), ADDRESS, CITY, ZIP, COUNTY, LAT, LON,
  PHOTO_URL, ALIAS_LAST_NAME_LIST

DOB, race, sex, height, weight, eye/hair color, and individual
offenses are NOT public.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, OffenderRecord, Registration, Source
from .base import Adapter

FEATURE_SERVER = (
    "https://services.arcgis.com/lnWnvGNQtyvDCFPs/arcgis/rest/services/"
    "Montana_SVOR_Web/FeatureServer/0"
)
PUBLIC_VIEWER = "https://app.doj.mt.gov/apps/svow/search/"
PAGE_SIZE = 2000  # server-side cap

# Placeholder photo URL the FeatureServer emits when an offender has no
# real image. Filter these out so we don't pull a "no photo" stub down
# 7K times.
_NO_PHOTO_MARKERS = (
    "Offender_NoPhoto",
    "no_photo",
    "NoPhoto.jpg",
)


class MontanaAdapter(Adapter):
    jurisdiction = "US-MT"
    source_name = "Montana DOJ Sexual or Violent Offender Registry"

    def __init__(
        self,
        page_size: int = PAGE_SIZE,
        request_timeout: float = 90.0,
        retry_attempts: int = 3,
        retry_backoff: float = 2.0,
    ) -> None:
        self.page_size = page_size
        self.request_timeout = request_timeout
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self._fetched_at: datetime | None = None
        self._client: httpx.Client | None = None

    # ---- fetch ---------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        """Pull every (PERS_SID, LOC_ID) row, then merge all rows for a
        given PERS_SID into a single payload. The merged payload is what
        normalize() consumes — one record per person, with every
        published address represented."""
        self._fetched_at = datetime.now(timezone.utc)
        self._client = httpx.Client(
            timeout=self.request_timeout,
            headers={"User-Agent": "Mozilla/5.0 registry-faces/0.1"},
        )
        try:
            by_pers: dict[str, dict] = {}
            offset = 0
            page_n = 0
            while True:
                rows = self._get_page(offset)
                if not rows:
                    break
                page_n += 1
                for row in rows:
                    attrs = row.get("attributes") or {}
                    sid = (attrs.get("PERS_SID") or "").strip()
                    if not sid:
                        continue
                    if sid not in by_pers:
                        by_pers[sid] = {
                            "pers_sid": sid,
                            "name": attrs.get("NAME"),
                            "off_typ": attrs.get("OFF_TYP"),
                            "tier_lvl": attrs.get("TIER_LVL"),
                            "designation": attrs.get("DESIGNATION"),
                            "noncompliant": attrs.get("NONCOMPLIANT"),
                            "photo_url": attrs.get("PHOTO_URL"),
                            "detail_url": attrs.get("DETAIL_URL"),
                            "alias_last_name_list": attrs.get("ALIAS_LAST_NAME_LIST"),
                            "loccount": attrs.get("LOCCOUNT"),
                            "locations": [],
                        }
                    # Append this row's address as one of the person's locations.
                    by_pers[sid]["locations"].append(
                        {
                            "loc_id": attrs.get("LOC_ID"),
                            "address": attrs.get("ADDRESS"),
                            "address_descr": attrs.get("ADDRESS_DESCR"),
                            "city": attrs.get("CITY"),
                            "zip": attrs.get("ZIP"),
                            "county": attrs.get("COUNTY"),
                            "lat": attrs.get("LATITUDE"),
                            "lon": attrs.get("LONGITUDE"),
                        }
                    )
                    # Keep the most-informative photo across duplicates.
                    if not _is_real_photo(by_pers[sid].get("photo_url")):
                        by_pers[sid]["photo_url"] = attrs.get("PHOTO_URL")
                print(
                    f"    MT page={page_n} offset={offset} rows={len(rows)} "
                    f"unique_so_far={len(by_pers)}",
                    flush=True,
                )
                if len(rows) < self.page_size:
                    break
                offset += self.page_size

            print(f"  MT: collected {len(by_pers)} unique offenders", flush=True)
            for sid in sorted(by_pers.keys()):
                yield by_pers[sid]
        finally:
            self._client.close()
            self._client = None

    def _get_page(self, offset: int) -> list[dict]:
        assert self._client is not None
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "false",
            "resultOffset": str(offset),
            "resultRecordCount": str(self.page_size),
            "orderByFields": "OBJECTID",
            "f": "json",
        }
        last_err: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                r = self._client.get(f"{FEATURE_SERVER}/query", params=params)
                r.raise_for_status()
                data = r.json()
                return data.get("features") or []
            except Exception as e:
                last_err = e
                time.sleep(self.retry_backoff * (attempt + 1))
        raise RuntimeError(
            f"MT FeatureServer page failed at offset {offset} after "
            f"{self.retry_attempts} attempts: {last_err}"
        ) from last_err

    # ---- normalize -----------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        sid = (raw.get("pers_sid") or "").strip()
        full_name = _flip_lastfirst(raw.get("name") or "")

        aliases = _split_alias_list(raw.get("alias_last_name_list"))

        identity = Identity(
            full_name=full_name or "UNKNOWN",
            aliases=aliases,
            # DOB/sex/race/etc. are not published by Montana.
        )

        addresses: list[Address] = []
        seen_keys: set[tuple] = set()
        for loc in raw.get("locations") or []:
            addr = _to_address(loc)
            if addr is None:
                continue
            key = (
                (addr.street or "").lower(),
                (addr.city or "").lower(),
                (addr.zip or "").lower(),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            addresses.append(addr)

        noncompliant = (raw.get("noncompliant") or "").strip().upper() == "Y"
        # Montana doesn't publish absconder/deceased/incarcerated state as a
        # status code. Non-compliant ≠ absconder per Montana's own term, so
        # we keep status="active" and surface the flag via Registration.
        registration = Registration(
            status="active",
            absconder=False,
        )
        _ = noncompliant  # preserved in raw for clients that care

        info_url = None
        detail = raw.get("detail_url")
        if detail:
            info_url = f"{PUBLIC_VIEWER}{detail}"

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=sid,
                source_url=FEATURE_SERVER,
                info_url=info_url,
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = (raw.get("photo_url") or "").strip()
        if not url or not _is_real_photo(url):
            return []
        if not url.startswith("http"):
            return []
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


# ---------------------------------------------------------------------------
# Field helpers


def _is_real_photo(url: Any) -> bool:
    if not url or not isinstance(url, str):
        return False
    if not url.startswith("http"):
        return False
    return not any(marker in url for marker in _NO_PHOTO_MARKERS)


def _flip_lastfirst(name: str) -> str:
    """`LAST, FIRST MIDDLE` → `FIRST MIDDLE LAST`."""
    if "," in name:
        last, rest = name.split(",", 1)
        return f"{rest.strip()} {last.strip()}".strip()
    return name.strip()


def _split_alias_list(value: Any) -> list[str]:
    """ALIAS_LAST_NAME_LIST is a comma/semicolon-separated string of
    aliased surnames. Split, trim, dedup."""
    if not value or not isinstance(value, str):
        return []
    parts = [p.strip() for chunk in value.split(";") for p in chunk.split(",")]
    out: list[str] = []
    seen = set()
    for p in parts:
        if not p:
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _to_address(loc: dict) -> Address | None:
    street = (loc.get("address") or "").strip() or None
    descr = (loc.get("address_descr") or "").strip() or None
    city = (loc.get("city") or "").strip() or None
    zip_code = loc.get("zip")
    if zip_code is not None:
        zip_code = str(zip_code).strip() or None
    lat = loc.get("lat")
    lon = loc.get("lon")
    if lat == 0 and lon == 0:
        lat, lon = None, None
    if not any((street, descr, city, zip_code)):
        return None
    return Address(
        type="home",
        street=street or descr,
        city=city,
        state="MT",
        zip=zip_code,
        lat=float(lat) if isinstance(lat, (int, float)) else None,
        lon=float(lon) if isinstance(lon, (int, float)) else None,
    )


def build() -> MontanaAdapter:
    return MontanaAdapter()
