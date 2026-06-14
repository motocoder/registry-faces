"""Georgia GBI sex offender registry adapter.

Source: https://state.sor.gbi.ga.gov/SORT_PUBLIC/sor.csv

GBI publishes the full Georgia SOR as a plain HTTP CSV download
(linked from `gbi.georgia.gov/services/georgia-sex-offender-registry`).
No captcha, no auth, no terms gate — just 5-6 MB of CSV. The per-
offender search portal at `state.sor.gbi.ga.gov/Sort_Public` does
have a "Conditions of Use" click-through and would be where photos
live; the CSV does not include photos, so this adapter ships without
them. Adding a photo-enrichment pass would mean walking the search
portal after the click-through.

Schema notes:
  * No native unique ID. `source_id` is synthesized from
    `NAME|YEAR OF BIRTH` so two CSV rows for the same person (e.g.
    multiple offenses) collapse into one record at upsert time. Schema
    merge unions their offenses by (raw_description, conviction_date).
  * Height is FDLE-style `feet*100 + inches` (e.g. "511" = 5'11"),
    same encoding as Florida/Tennessee.
  * Only year of birth is published (not full DOB).
  * Flags: INCARCERATED / PREDATOR / ABSCONDER are emitted as the
    literal flag word ("INCARCERATED" / "PREDATOR" / "ABSCONDER") or
    a single space (no flag). LEVELING is "LEVEL 1/2", "NOT LEVELED",
    "CANNOT LEVEL", or "SEXUALLY DANGEROUS PREDATOR".
  * The cached CSV lives at `registry-runs/georgia/sor.csv` and is
    refetched if older than `cache_max_age_hours` (default 24).
"""

from __future__ import annotations

import csv
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, Offense, OffenderRecord, Registration, Source
from .base import Adapter

DOWNLOAD_URL = "https://state.sor.gbi.ga.gov/SORT_PUBLIC/sor.csv"
DEFAULT_CACHE_PATH = Path("registry-runs/georgia/sor.csv")
DEFAULT_CACHE_MAX_AGE_HOURS = 24

# SEX column → canonical Sex literal
_SEX_MAP = {
    "MALE": "M",
    "FEMALE": "F",
    "M": "M",
    "F": "F",
}


class GeorgiaAdapter(Adapter):
    jurisdiction = "US-GA"
    source_name = "Georgia Bureau of Investigation"

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
        with self.csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield row

    def _ensure_csv(self) -> None:
        if self.csv_path.exists() and not self.force_refresh:
            age_seconds = time.time() - self.csv_path.stat().st_mtime
            if age_seconds < self.cache_max_age_hours * 3600:
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

        name = (raw.get("NAME") or "").strip()
        full_name = _flip_lastfirst(name)
        yob = _parse_int(raw.get("YEAR OF BIRTH"))

        # Synthesize source_id; same person with multiple offenses collapses
        # into one record at upsert time and the schema merge unions offenses.
        source_id = f"{name}|{yob or ''}"

        identity = Identity(
            full_name=full_name or "UNKNOWN",
            year_of_birth=yob,
            sex=_SEX_MAP.get((raw.get("SEX") or "").strip().upper(), "unknown"),
            race=(raw.get("RACE") or "").strip() or None,
            height_cm=_height_cm(raw.get("HEIGHT")),
            weight_kg=_weight_kg(raw.get("WEIGHT")),
            eye_color=(raw.get("EYE COLOR") or "").strip() or None,
            hair_color=(raw.get("HAIR COLOR") or "").strip() or None,
        )

        addresses: list[Address] = []
        addr = _build_address(raw)
        if addr is not None:
            addresses.append(addr)

        offenses: list[Offense] = []
        crime = (raw.get("CRIME") or "").strip()
        if crime:
            offenses.append(
                Offense(
                    raw_description=crime,
                    conviction_date=_parse_us_date(raw.get("CONVICTION DATE")),
                    jurisdiction=(raw.get("CONVICTION STATE") or "").strip() or None,
                    tier_or_level_raw=(raw.get("LEVELING") or "").strip() or None,
                )
            )

        absconder = (raw.get("ABSCONDER") or "").strip().upper() == "ABSCONDER"
        incarcerated = (raw.get("INCARCERATED") or "").strip().upper() == "INCARCERATED"
        if absconder:
            status = "absconder"
        elif incarcerated:
            status = "incarcerated"
        else:
            status = "active"
        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=absconder,
            registered_since=_parse_us_date(raw.get("REGISTRATION DATE")),
            next_verification=_parse_us_date(raw.get("RES VERIFICATION DATE")),
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
            offenses=offenses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # GBI doesn't publish photos in the CSV. Per-person photos live
        # behind the Conditions-of-Use click-through on
        # state.sor.gbi.ga.gov/Sort_Public; a future second-pass
        # adapter could harvest those.
        return []


# ---------------------------------------------------------------------------
# Field helpers


def _flip_lastfirst(name: str) -> str:
    """`LAST, FIRST MIDDLE` → `FIRST MIDDLE LAST`."""
    if "," in name:
        last, rest = name.split(",", 1)
        return f"{rest.strip()} {last.strip()}".strip()
    return name.strip()


def _parse_int(value: object) -> int | None:
    if not value:
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    return int(s)


def _parse_us_date(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _height_cm(value: object) -> float | None:
    """FDLE-style: feet*100 + inches (e.g. "511" = 5'11")."""
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


def _build_address(raw: dict) -> Address | None:
    num = (raw.get("STREET NUMBER") or "").strip()
    street = (raw.get("STREET") or "").strip()
    city = (raw.get("CITY") or "").strip()
    state = (raw.get("STATE") or "").strip()
    zip_code = (raw.get("ZIP CODE") or "").strip()
    county = (raw.get("COUNTY") or "").strip()
    line = " ".join(p for p in (num, street) if p) or None
    if not any((line, city, state, zip_code)):
        return None
    _ = county  # preserved in raw
    return Address(
        type="home",
        street=line,
        city=city or None,
        state=state or None,
        zip=zip_code or None,
    )


def build() -> GeorgiaAdapter:
    return GeorgiaAdapter()
