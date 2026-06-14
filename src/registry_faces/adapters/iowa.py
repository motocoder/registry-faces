"""Iowa state registry adapter — direct public JSON API.

Source: https://www.iowasexoffender.gov/api/

Iowa publishes a fully documented public JSON API (see
`/feed/info`). No captcha, no auth, no acceptance gate — just rate
limits documented in the TOS:

  * 50 requests/hour
  * 100 results/page maximum
  * 14-day cache TTL
  * Lat/lon may NOT be cached for any purpose beyond immediate map
    rendering (we strip lat/lon on save to comply)

Schema notes:
  * Search results include: DOB, address (line_1/2, city, postal_code,
    state, county), gender, race, height/weight/eye/hair/skin_tone,
    tier, residency/employment/exclusion-zone flags, victim flags,
    photo URL(s), `last_changed`.
  * Search results do NOT include `convictions`, `aliases`, or
    `skin_markings` — those are only returned when narrowing to a
    single registrant via `/api/registrant/<id>.json`. Fetching detail
    for all 6,753 records at the 50/hr limit would take ~5 days, so
    we skip detail enrichment and ship with the search-result data.
  * `registrant` is the system identity → `source.source_id`.
  * Multiple photo URLs published per person (`photos` array).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, Offense, OffenderRecord, Registration, Source
from .base import Adapter

API_BASE = "https://www.iowasexoffender.gov"
SEARCH_URL = f"{API_BASE}/api/search/results.json"
DETAIL_BASE = f"{API_BASE}/registrant/registrantdetail"
PAGE_SIZE = 100  # API hard cap
# Per the published TOS: 50 requests/hour ≈ 72s/req. We use 75s to add a
# safety margin and avoid burning into the cap if a retry fires.
DEFAULT_REQUEST_DELAY_S = 75.0


class IowaAdapter(Adapter):
    jurisdiction = "US-IA"
    source_name = "Iowa Sex Offender Registry"

    def __init__(
        self,
        request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
        request_timeout: float = 60.0,
        retry_attempts: int = 3,
        retry_backoff: float = 5.0,
        progress_every: int = 5,
    ) -> None:
        self.request_delay_s = request_delay_s
        self.request_timeout = request_timeout
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self.progress_every = progress_every
        self._fetched_at: datetime | None = None
        self._client: httpx.Client | None = None

    def fetch(self) -> Iterator[dict]:
        self._fetched_at = datetime.now(timezone.utc)
        self._client = httpx.Client(
            timeout=self.request_timeout,
            headers={
                "User-Agent": "Mozilla/5.0 registry-faces/0.1",
                "Accept": "application/json",
            },
        )
        try:
            page = 1
            yielded = 0
            while True:
                data = self._get_page(page)
                if not data:
                    break
                records = data.get("records") or []
                total = data.get("results")
                if not records:
                    break
                for rec in records:
                    yield rec
                yielded += len(records)
                if page == 1 and total is not None:
                    print(f"  IA: total reported {total} records, paging at {PAGE_SIZE}/page", flush=True)
                if page % self.progress_every == 0:
                    print(f"    IA page={page} yielded={yielded} of {total}", flush=True)
                if total is not None and yielded >= int(total):
                    break
                # The API stops paginating naturally when records < PAGE_SIZE.
                if len(records) < PAGE_SIZE:
                    break
                page += 1
                time.sleep(self.request_delay_s)
            print(f"  IA done: {yielded} records across {page} page(s)", flush=True)
        finally:
            self._client.close()
            self._client = None

    def _get_page(self, page: int) -> dict | None:
        assert self._client is not None
        params = {
            "stateabbr": "IA",
            "per_page": PAGE_SIZE,
            "page": page,
        }
        last_err: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                r = self._client.get(SEARCH_URL, params=params)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                time.sleep(self.retry_backoff * (attempt + 1))
        print(f"  IA page {page} failed: {last_err}", flush=True)
        return None

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        registrant = str(raw.get("registrant") or "").strip()
        first = (raw.get("first_name") or "").strip()
        middle = (raw.get("middle_name") or "").strip()
        last = (raw.get("last_name") or "").strip()
        suffix = (raw.get("suffix") or "").strip()
        full_name = " ".join(p for p in (first, middle, last, suffix) if p) or "UNKNOWN"

        dob = _parse_us_date(raw.get("birthdate"))
        height_in = raw.get("height_inches")
        weight_lb = raw.get("weight_pounds")

        identity = Identity(
            full_name=full_name,
            aliases=[],  # search response always returns []
            dob=dob,
            year_of_birth=dob.year if dob else None,
            sex=_sex(raw.get("gender")),
            race=(raw.get("race") or "").strip() or None,
            height_cm=_inches_to_cm(height_in),
            weight_kg=_pounds_to_kg(weight_lb),
            eye_color=(raw.get("eye_color") or "").strip() or None,
            hair_color=(raw.get("hair_color") or "").strip() or None,
        )

        # Address — note TOS forbids caching lat/lon, so we drop them here.
        addresses: list[Address] = []
        line_1 = (raw.get("line_1") or "").strip()
        line_2 = (raw.get("line_2") or "").strip()
        city = (raw.get("city") or "").strip()
        postal = (raw.get("postal_code") or "").strip()
        state = (raw.get("state") or "").strip()
        street = ", ".join(p for p in (line_1, line_2) if p) or None
        if any((street, city, state, postal)):
            addresses.append(
                Address(
                    type="home",
                    street=street,
                    city=city or None,
                    state=state or "IA",
                    zip=postal or None,
                )
            )

        # Offense placeholder — convictions aren't in the search payload.
        # Surface the per-person tier on a synthetic offense so it round-trips
        # in our schema; raw payload is preserved for re-derivation.
        offenses: list[Offense] = []
        tier = (raw.get("tier") or "").strip()
        if tier:
            offenses.append(
                Offense(
                    raw_description="(see source: convictions only available via /api/registrant/<id>.json)",
                    tier_or_level_raw=tier,
                )
            )

        wanted = str(raw.get("wanted") or "0").strip() == "1"
        status = "absconder" if wanted else "active"
        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=wanted,
        )

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=registrant,
                source_url=API_BASE,
                info_url=f"{DETAIL_BASE}?id={registrant}",
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # Iowa publishes a `photo` (current primary) PLUS a `photos`
        # array of historical mugshots — typically 10-20 per person.
        # Only the primary is useful for face matching; the history
        # would inflate shards ~15× without adding signal.
        primary = (raw.get("photo") or "").strip()
        if not primary.startswith("http"):
            return []
        return [
            PhotoRef(
                url=primary,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


# ---------------------------------------------------------------------------
# Field helpers


def _sex(value: object) -> str:
    v = str(value or "").strip().upper()
    if v in {"M", "MALE"}:
        return "M"
    if v in {"F", "FEMALE"}:
        return "F"
    return "unknown"


def _parse_us_date(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _inches_to_cm(value: object) -> float | None:
    try:
        inches = int(value)
    except (TypeError, ValueError):
        return None
    if not (36 <= inches <= 96):
        return None
    return round(inches * 2.54, 1)


def _pounds_to_kg(value: object) -> float | None:
    try:
        pounds = int(value)
    except (TypeError, ValueError):
        return None
    if not (1 <= pounds <= 1000):
        return None
    return round(pounds * 0.45359237, 1)


def build() -> IowaAdapter:
    return IowaAdapter()
