"""Florida FDLE sexual offender/predator registry adapter.

Source: https://offender.fdle.state.fl.us

FDLE publishes a complete bulk CSV at `/offender/publicDataFile.jsf`. The
browser-facing flow asks for a 4-character image captcha, but FDLE's own
public memo documents an `?email=...&mode=auto` query string that
bypasses the captcha for automated downloads. We use that — no browser
or captcha solver is required.

The CSV is regenerated every 4 hours per the memo. `fetch()` caches the
download at `registry-runs/florida/public_data_file.csv` and reuses it
unless the file is older than `cache_max_age_hours` (default 4 hours) or
the caller passes `force_refresh=True`.

Each CSV row is one registrant + up to three addresses (permanent /
temporary / transient). `PERSON_NBR` is the FDLE-assigned primary key
and becomes our `source_id`.

Override the email used for the auto-download via the
`REGISTRY_FACES_FL_EMAIL` env var, or by constructing the adapter
directly with `email=...`. Default falls back to a placeholder.
"""

from __future__ import annotations

import csv
import os
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, OffenderRecord, Registration, Source
from .base import Adapter

DOWNLOAD_URL = "https://offender.fdle.state.fl.us/offender/publicDataFile.jsf"
DEFAULT_CACHE_PATH = Path("registry-runs/florida/public_data_file.csv")
DEFAULT_CACHE_MAX_AGE_HOURS = 4

# STATUS column -> canonical Registration.status.
_STATUS_MAP = {
    "Absconded": "absconder",
    "Civil Commitment": "incarcerated",
    "Confinement": "incarcerated",
    "Deceased": "deceased",
    "Deported": "removed",
    "Released - Subject to Registration": "active",
    "Supervised - FL Dept of Corrections": "active",
    "Supervised - FL Dept of Juvenile Justice": "active",
    "Supervised - US Probation": "active",
}

# SEX column values: M, F, U. Canonical Sex literal allows X but the CSV
# never produces it; map U to "unknown".
_SEX_MAP = {"M": "M", "F": "F", "U": "unknown"}


class FloridaAdapter(Adapter):
    jurisdiction = "US-FL"
    source_name = "Florida Department of Law Enforcement"

    def __init__(
        self,
        email: str | None = None,
        csv_path: Path | str | None = None,
        cache_max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS,
        force_refresh: bool = False,
    ) -> None:
        self.email = (
            email
            or os.environ.get("REGISTRY_FACES_FL_EMAIL")
            or "registry-faces@example.com"
        )
        self.csv_path = Path(csv_path) if csv_path else DEFAULT_CACHE_PATH
        self.cache_max_age_hours = cache_max_age_hours
        self.force_refresh = force_refresh
        self._fetched_at: datetime | None = None

    def fetch(self) -> Iterator[dict]:
        self._ensure_csv()
        self._fetched_at = datetime.fromtimestamp(
            self.csv_path.stat().st_mtime, tz=timezone.utc
        )
        with self.csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield row

    def _ensure_csv(self) -> None:
        if self.csv_path.exists() and not self.force_refresh:
            age_seconds = time.time() - self.csv_path.stat().st_mtime
            if age_seconds < self.cache_max_age_hours * 3600:
                return

        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        params = {"email": self.email, "mode": "auto"}
        with httpx.Client(
            follow_redirects=True,
            timeout=300,
            headers={"User-Agent": "Mozilla/5.0 registry-faces/0.1"},
        ) as client:
            with client.stream("GET", DOWNLOAD_URL, params=params) as resp:
                resp.raise_for_status()
                tmp = self.csv_path.with_suffix(self.csv_path.suffix + ".part")
                with tmp.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
                tmp.replace(self.csv_path)

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        source_id = raw["PERSON_NBR"].strip()
        full_name = _join_name(
            raw.get("FIRST_NAME"),
            raw.get("MIDDLE_NAME"),
            raw.get("LAST_NAME"),
            raw.get("SUFFIX_NAME"),
        )

        dob = _parse_date(raw.get("BIRTH_DATE"))

        identity = Identity(
            full_name=full_name,
            dob=dob,
            year_of_birth=dob.year if dob else None,
            sex=_SEX_MAP.get((raw.get("SEX") or "").strip().upper(), "unknown"),
            race=(raw.get("RACE") or "").strip() or None,
            height_cm=_height_cm(raw.get("HEIGHT")),
            weight_kg=_weight_kg(raw.get("WEIGHT")),
            eye_color=(raw.get("EYE_COLOR") or "").strip() or None,
            hair_color=(raw.get("HAIR") or "").strip() or None,
        )

        addresses: list[Address] = []
        for kind, addr_type, date_key in (
            ("PERM", "home", "PERM_ADDRESS_ADDED"),
            ("TEMP", "temporary", "TEMP_ADDRESS_ADDED"),
            ("TRANS", "other", "TRANS_ADDRESS_ADDED"),
        ):
            addr = _address(raw, kind, addr_type, date_key)
            if addr is not None:
                addresses.append(addr)

        status_raw = (raw.get("STATUS") or "").strip()
        status = _STATUS_MAP.get(status_raw, "unknown")
        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=(status == "absconder"),
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
        url = (raw.get("IMAGE_URL") or "").strip()
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


def _join_name(first: str | None, middle: str | None, last: str | None, suffix: str | None) -> str:
    parts = [(p or "").strip() for p in (first, middle, last, suffix)]
    name = " ".join(p for p in parts if p)
    return name or "UNKNOWN"


def _parse_date(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _height_cm(value: object) -> float | None:
    """FDLE stores height as feet*100 + inches (e.g. 511 = 5'11", 602 = 6'2")."""
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


def _address(raw: dict, prefix: str, addr_type: str, date_key: str) -> Address | None:
    street_1 = (raw.get(f"{prefix}_ADDRESS_LINE_1") or "").strip()
    street_2 = (raw.get(f"{prefix}_ADDRESS_LINE_2") or "").strip()
    city = (raw.get(f"{prefix}_CITY") or "").strip()
    state = (raw.get(f"{prefix}_STATE") or "").strip()
    zip5 = (raw.get(f"{prefix}_ZIP5") or "").strip()
    zip4 = (raw.get(f"{prefix}_ZIP4") or "").strip()
    if not any((street_1, street_2, city, state, zip5)):
        return None
    street = ", ".join(p for p in (street_1, street_2) if p) or None
    if zip5 and zip4:
        zip_code: str | None = f"{zip5}-{zip4}"
    else:
        zip_code = zip5 or None
    return Address(
        type=addr_type,  # type: ignore[arg-type]
        street=street,
        city=city or None,
        state=state or None,
        zip=zip_code,
        verified_at=_parse_date(raw.get(date_key)),
    )


def build() -> FloridaAdapter:
    return FloridaAdapter()
