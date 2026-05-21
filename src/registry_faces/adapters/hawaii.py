"""Hawaii state registry adapter — currently non-functional.

This adapter is preserved as a structural example only. Investigation in
2026-05 found that:

  * `https://hcjdc.ehawaii.gov/bulkcor/` requires HCJDC registration / SSO
    login — it's not a public bulk download as the original code assumed.
  * The actually-public Hawaii registry at
    `https://sexoffenders.ehawaii.gov/coveredoffender/` is gated behind
    Google reCAPTCHA. Scraping that is brittle and adversarial.

So this adapter does not run. `fetch()` raises immediately with a useful
message. For a working reference adapter, see `south_dakota.py`.

If Hawaii ever publishes a real public bulk endpoint, update `BULK_URL`
and remove the `raise` in `fetch()`.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, Offense, OffenderRecord, Source
from .base import Adapter

BULK_URL = "https://hcjdc.ehawaii.gov/bulkcor/public/csv"


class HawaiiAdapter(Adapter):
    jurisdiction = "US-HI"
    source_name = "Hawaii Criminal Justice Data Center"

    def __init__(self, bulk_url: str = BULK_URL) -> None:
        self.bulk_url = bulk_url
        self._fetched_at: datetime | None = None

    def fetch(self) -> Iterator[dict]:
        raise RuntimeError(
            "Hawaii registry has no public bulk endpoint. "
            "hcjdc.ehawaii.gov/bulkcor/ requires SSO login; "
            "sexoffenders.ehawaii.gov is gated behind reCAPTCHA. "
            "See module docstring. Use south_dakota for a working reference."
        )
        # Keep the stream/CSV scaffolding visible below as a reference for
        # future Hawaii integration if a public bulk URL ever appears.
        yield {}  # pragma: no cover

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        full_name = " ".join(
            part
            for part in (raw.get("first_name"), raw.get("middle_name"), raw.get("last_name"))
            if part
        ).strip() or raw.get("name", "UNKNOWN")

        source_id = (
            raw.get("registration_number") or raw.get("offender_id") or full_name
        )

        addresses: list[Address] = []
        if raw.get("residence_street"):
            addresses.append(
                Address(
                    type="home",
                    street=raw.get("residence_street"),
                    city=raw.get("residence_city"),
                    state=raw.get("residence_state") or "HI",
                    zip=raw.get("residence_zip"),
                )
            )

        offenses: list[Offense] = []
        if raw.get("offense_description"):
            offenses.append(
                Offense(
                    raw_code=raw.get("offense_code"),
                    raw_description=raw["offense_description"],
                    jurisdiction="US-HI",
                    statute=raw.get("statute"),
                    tier_or_level_raw=raw.get("tier"),
                )
            )

        return OffenderRecord(
            source=Source(
                jurisdiction="US-HI",
                source_id=str(source_id),
                source_url=self.bulk_url,
                fetched_at=self._fetched_at,
            ),
            identity=Identity(
                full_name=full_name,
                aliases=[
                    a.strip() for a in (raw.get("aliases") or "").split(";") if a.strip()
                ],
                year_of_birth=_safe_int(raw.get("year_of_birth")),
                sex=_normalize_sex(raw.get("sex")),
                race=raw.get("race"),
                eye_color=raw.get("eye_color"),
                hair_color=raw.get("hair_color"),
            ),
            addresses=addresses,
            offenses=offenses,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = raw.get("photo_url")
        if not url:
            return []
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _normalize_sex(value: object) -> str:
    if not value:
        return "unknown"
    v = str(value).strip().upper()
    if v in {"M", "MALE"}:
        return "M"
    if v in {"F", "FEMALE"}:
        return "F"
    return "unknown"


def build() -> HawaiiAdapter:
    return HawaiiAdapter()
