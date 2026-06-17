"""Oregon state registry adapter — direct bulk CSV.

Source: https://sexoffenders.osp.oregon.gov/sorpublicapi/api/offenders.csv

OSP runs a React SPA at `sexoffenders.osp.oregon.gov/sorpublic/` for
public search; the search forms go through reCAPTCHA. But the SPA
fetches its initial offender list as plain CSV at
`/sorpublicapi/api/offenders.csv` — no captcha, no acceptance gate,
no auth.

Important context from the sibling `offender-count` endpoint:
  {"totalPublishedCount": 1976, "totalCount": 34489}

Oregon has ~34K total registrants, but state law (ORS 181.812) only
makes a small subset publicly disclosable. The CSV is exactly that
publishable subset — ~2K records — not the full registry. That's an
upstream policy choice, not a coverage gap on our side.

Schema notes:
  * No native unique ID. We synthesize from
    `FirstName MiddleName LastName Suffix | DOB` so re-ingest is
    idempotent.
  * Height is FDLE-style `feet*100 + inches` (e.g. "507" = 5'7").
  * `Offender Status Code(s)` is a comma-separated multi-value string:
    SEX OFFENDER REGISTRATION / NON-COMPLIANT - ANNUAL... /
    NON-COMPLIANT - ADDRESS / PRISON - INCARCERATED / NON-RESIDENT /
    PRISON - PAROLE / TRANSIENT. We map to canonical status with
    priority: any "ABSCONDER" / non-compliant ⇒ absconder,
    "INCARCERATED" ⇒ incarcerated, otherwise active.
  * No race / sex / photos / offenses are published in the CSV.
"""

from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, OffenderRecord, Registration, Source
from .base import Adapter

DOWNLOAD_URL = (
    "https://sexoffenders.osp.oregon.gov/sorpublicapi/api/offenders.csv"
)
DEFAULT_CACHE_PATH = Path("registry-runs/oregon/offenders.csv")
DEFAULT_CACHE_MAX_AGE_HOURS = 24


class OregonAdapter(Adapter):
    jurisdiction = "US-OR"
    source_name = "Oregon State Police"

    def __init__(
        self,
        csv_path: Path | str | None = None,
        cache_max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS,
        force_refresh: bool = False,
    ) -> None:
        self.csv_path = Path(csv_path) if csv_path else DEFAULT_CACHE_PATH
        self.cache_max_age_hours = cache_max_age_hours
        self.force_refresh = force_refresh
        self._fetched_at: datetime | None = None

    def fetch(self) -> Iterator[dict]:
        self._ensure_csv()
        self._fetched_at = datetime.fromtimestamp(
            self.csv_path.stat().st_mtime, tz=timezone.utc
        )
        # Oregon publishes the CSV as ISO-8859-1 (per Content-Type header)
        with self.csv_path.open(newline="", encoding="ISO-8859-1") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield row

    def _ensure_csv(self) -> None:
        if self.csv_path.exists() and not self.force_refresh:
            age = time.time() - self.csv_path.stat().st_mtime
            if age < self.cache_max_age_hours * 3600:
                return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(
            timeout=300,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 registry-faces/0.1"},
        ) as client:
            with client.stream("GET", DOWNLOAD_URL) as resp:
                resp.raise_for_status()
                tmp = self.csv_path.with_suffix(self.csv_path.suffix + ".part")
                with tmp.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
                tmp.replace(self.csv_path)

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        first = (raw.get("First Name") or "").strip()
        middle = (raw.get("Middle Name") or "").strip()
        last = (raw.get("Last Name") or "").strip()
        suffix = (raw.get("Suffix") or "").strip()
        full_name = " ".join(p for p in (first, middle, last, suffix) if p) or "UNKNOWN"

        dob = _parse_us_date(raw.get("Date of Birth"))
        source_id = f"{full_name}|{_key_dob(raw.get('Date of Birth'))}"

        identity = Identity(
            full_name=full_name,
            dob=dob,
            year_of_birth=dob.year if dob else None,
            height_cm=_height_cm(raw.get("Height")),
            weight_kg=_weight_kg(raw.get("Weight")),
            hair_color=(raw.get("Hair") or "").strip() or None,
            eye_color=(raw.get("Eyes") or "").strip() or None,
        )

        addresses: list[Address] = []
        street = (raw.get("Residence Street Address") or "").strip()
        apt = (raw.get("Residence Apartment Number/Suite Number") or "").strip()
        city = (raw.get("Residence City") or "").strip()
        state = (raw.get("Residence State") or "").strip() or "OR"
        zip_code = (raw.get("Residence Zip") or "").strip()
        county = (raw.get("Residence County") or "").strip()
        line = " ".join(p for p in (street, apt) if p) or None
        if any((line, city, zip_code)):
            addresses.append(
                Address(
                    type="home",
                    street=line,
                    city=city or None,
                    state=state,
                    zip=zip_code or None,
                )
            )
            _ = county  # preserved in raw

        codes = [c.strip() for c in (raw.get("Offender Status Code(s)") or "").split(",")]
        codes = [c for c in codes if c]
        status, absconder = _classify_status(codes)
        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=absconder,
        )

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=source_id,
                source_url=DOWNLOAD_URL,
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # OSP does not publish photos in the bulk CSV. The per-person
        # detail pages in the SPA do display mugshots but those are
        # gated behind reCAPTCHA.
        return []


# ---------------------------------------------------------------------------
# Field helpers


def _classify_status(codes: list[str]) -> tuple[str, bool]:
    """Map Oregon's multi-value status codes to canonical + absconder flag.

    Priority order matches Oregon's intent: a person who's both
    incarcerated AND non-compliant historically gets the more concerning
    flag surfaced.
    """
    joined = " ".join(codes).upper()
    if "ABSCONDER" in joined or "NON-COMPLIANT" in joined:
        return "absconder", True
    if "INCARCERATED" in joined:
        return "incarcerated", False
    if "DECEASED" in joined:
        return "deceased", False
    return "active", False


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


def _key_dob(value: object) -> str:
    dt = _parse_us_date(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d")


def _height_cm(value: object) -> float | None:
    """OR encodes height as `feet*100 + inches`, e.g. "507" = 5'7"."""
    if not value:
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    n = int(s)
    feet, inches = divmod(n, 100)
    if not (3 <= feet <= 8) or not (0 <= inches <= 11):
        return None
    return round((feet * 12 + inches) * 2.54, 1)


def _weight_kg(value: object) -> float | None:
    if not value:
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    pounds = int(s)
    if pounds <= 0 or pounds > 1000:
        return None
    return round(pounds * 0.45359237, 1)


def build() -> OregonAdapter:
    return OregonAdapter()
