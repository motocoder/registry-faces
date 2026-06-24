"""Maldives — Prosecutor General's Office "Aamahi" child sex offenders registry.

The Maldives is one of the rare non-US jurisdictions that runs an OFFICIAL,
public, enumerable registry of sex offenders. Article 57 of the 2009 Special
Provisions Act to Deal with Child Sex Abuse Offenders mandates the Prosecutor
General's Office to publish an offender registry for public safety. It is served
publicly (no login, no CAPTCHA) at:

    http://offenders.mv/   ->  https://aamahi.pgo.mv/child-offenders

The page is a Vue SPA backed by an unauthenticated JSON endpoint that returns
the full register in one envelope (`{"data": [...], "total": N}`):

    POST https://aamahi.pgo.mv/api/child-offenders/list-new
    body: {"page": 1, "filters": {}}

Each record carries the person's name (Dhivehi script), national ID or passport,
nationality, permanent address, date of birth, current detention location, one
or more `verdicts` (offence label + judgement date + penalty), and a `file_url`
pointing at a portrait the registry itself serves (gov.pgo.mv, image/png).

Photos: `file_url` is a real source-served image URL, so `extract_photos`
returns it as a `PhotoRef`. The whole payload is preserved in `record.raw`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from registry_faces.schema import (
    Address,
    Identity,
    Offense,
    OffenderRecord,
    Registration,
    Source,
)
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

PUBLIC_URL = "http://offenders.mv/"
APP_URL = "https://aamahi.pgo.mv/child-offenders"
LIST_API = "https://aamahi.pgo.mv/api/child-offenders/list-new"
USER_AGENT = (
    "registry-faces/1.0 (+public PGO Maldives child sex-offender index)"
)


def _clean(value) -> str | None:
    """Collapse whitespace; return None for empty/blank values."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _parse_date(value) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None


class MvRegistryAdapter(Adapter):
    jurisdiction = "MV"
    source_name = (
        "Maldives Prosecutor General's Office (Aamahi) — child sex "
        "offenders registry"
    )

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=60.0,
            follow_redirects=True,
            # The registry's TLS chain is misconfigured for the offenders.mv
            # alias; the underlying pgo.mv host is the official endpoint.
            verify=False,
        )

    # -- fetch ----------------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        # The endpoint returns the entire register in one response (`data` holds
        # all rows, `total` is the count); `page`/`filters` are accepted but the
        # default unfiltered request enumerates everyone. Request page 1 and
        # iterate the full list deterministically.
        resp = self._client.post(LIST_API, json={"page": 1, "filters": {}})
        resp.raise_for_status()
        body = resp.json()
        for raw in body.get("data") or []:
            yield raw

    # -- normalize ------------------------------------------------------------

    def _source_id(self, raw: dict) -> str:
        # Every record carries exactly one government identifier: a Maldivian
        # national ID (`nid`) for nationals or a `passport` for foreigners.
        # Fall back to a deterministic hash of name + DOB so the rare record
        # missing both still gets a stable key.
        sid = _clean(raw.get("nid")) or _clean(raw.get("passport"))
        if sid:
            return sid
        seed = f"{_clean(raw.get('person_name_div'))}|{_clean(raw.get('date_of_birth'))}"
        return "anon-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)

        dob = _parse_date(raw.get("date_of_birth"))
        identity = Identity(
            full_name=_clean(raw.get("person_name_div")) or "(unknown)",
            dob=dob,
            year_of_birth=dob.year if dob else None,
            # Nationality is published in Dhivehi; keep it as free-text context.
            description=_clean(raw.get("country")),
        )

        addresses: list[Address] = []
        permanent = _clean(raw.get("permanent_house_name"))
        if permanent:
            addresses.append(
                Address(type="home", street=permanent, country="MV")
            )
        # Current detention/monitoring location (e.g. a named jail).
        location = _clean(raw.get("location"))
        if location:
            addresses.append(
                Address(type="other", street=location, country="MV")
            )

        # One Offense per verdict. The label is the offence description (Dhivehi);
        # judgement_date is the conviction date. No comparable tier/level code is
        # published, so `tier_or_level_raw` stays unset.
        offenses: list[Offense] = []
        for verdict in raw.get("verdicts") or []:
            description = _clean(verdict.get("label"))
            if not description:
                continue
            offenses.append(
                Offense(
                    raw_description=description,
                    conviction_date=_parse_date(verdict.get("judgement_date")),
                    jurisdiction="MV",
                )
            )

        # `location` set => currently held; `monitoring` flag => under
        # supervision; otherwise unknown. Codes are not normalized further.
        if location:
            status = "incarcerated"
        elif raw.get("monitoring"):
            status = "active"
        else:
            status = "unknown"

        return OffenderRecord(
            source=Source(
                jurisdiction="MV",
                source_id=self._source_id(raw),
                source_url=PUBLIC_URL,
                info_url=APP_URL,
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=Registration(status=status),
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = _clean(raw.get("file_url"))
        if not url:
            return []
        return [PhotoRef(url=url)]


def build() -> MvRegistryAdapter:
    return MvRegistryAdapter()
