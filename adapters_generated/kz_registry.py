"""Kazakhstan — Prosecutor General's Committee on Legal Statistics (KPSSU)
public database of persons convicted of sexual offences against minors.

Source type: public ArcGIS FeatureServer behind an Esri Experience Builder map.

Kazakhstan is one of the rare non-US jurisdictions that runs an OFFICIAL,
public, enumerable registry of sex offenders. The Committee on Legal Statistics
and Special Records of the Prosecutor General's Office (КПСиСУ / KPSSU) publishes
adults (18+) convicted of sexual offences against young children (<14) on a
public map:

    https://gis.kgp.kz/arcgis/apps/experiencebuilder/experience/?id=c048e1f975084dc1957108c00c9fb4d7

The map's "преступления против несовершеннолетних" page is backed by an
unauthenticated ArcGIS REST layer that returns a standard Esri query envelope
(`features[].attributes` + `exceededTransferLimit`), supports pagination
(`resultOffset`/`resultRecordCount`), and serves name, patronymic, date of
birth, sex, registration address (full string + область/район/город split) and
a portrait:

    GET .../KPSSU/peds_new/FeatureServer/0/query?where=1=1&outFields=*&f=json

Photos: each portrait is inlined as raw base64 in the `photo` field — there is
NO separate image URL the source serves. Per scope, `extract_photos` only
returns source-published image *URLs*; with none available it returns an empty
list. The base64 stays untouched in `record.raw` for a later step.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import httpx

from registry_faces.schema import (
    Address,
    Identity,
    OffenderRecord,
    Registration,
    Source,
)
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://gis.kgp.kz/arcgis"
LAYER = f"{BASE}/rest/services/KPSSU/peds_new/FeatureServer/0"
QUERY = f"{LAYER}/query"
EXPERIENCE = (
    f"{BASE}/apps/experiencebuilder/experience/"
    "?id=c048e1f975084dc1957108c00c9fb4d7"
)
USER_AGENT = (
    "registry-faces/1.0 (+public KPSSU sex-offender-against-minors index)"
)

# Layer maxRecordCount is 2000; stay well under it and page explicitly.
PAGE_SIZE = 500
# Safety bound only: ~540 records is ~2 pages. Paging stops on a short/empty
# page; this cap merely prevents a runaway loop if the envelope shape changes.
MAX_PAGES = 200

# Epoch base for the layer's `birthday` field (milliseconds since 1970, and it
# can be negative). Built additively so pre-1970 dates work cross-platform —
# datetime.fromtimestamp rejects negatives on Windows.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Cyrillic sex tokens used by the registry (`МУЖ.` / `ЖЕН.`).
_SEX = {"МУЖ": "M", "ЖЕН": "F"}


def _sex(value: str | None) -> str:
    token = (value or "").strip().upper().rstrip(".")
    return _SEX.get(token, "unknown")


def _from_epoch_ms(value) -> datetime | None:
    if value is None:
        return None
    try:
        return _EPOCH + timedelta(milliseconds=int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _clean(value: str | None) -> str | None:
    """Collapse whitespace; the registry pads some fields with a lone space."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


class KzRegistryAdapter(Adapter):
    jurisdiction = "KZ"
    source_name = (
        "Kazakhstan Prosecutor General's Committee on Legal Statistics "
        "(KPSSU) — register of sexual offences against minors"
    )

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=60.0,
            follow_redirects=True,
        )

    # -- fetch ----------------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        for page in range(MAX_PAGES):
            resp = self._client.get(
                QUERY,
                params={
                    "where": "1=1",
                    "outFields": "*",
                    "returnGeometry": "true",
                    # Stable ordering makes the run deterministic across pages.
                    "orderByFields": "objectid",
                    "resultOffset": page * PAGE_SIZE,
                    "resultRecordCount": PAGE_SIZE,
                    "f": "json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            features = body.get("features") or []
            if not features:
                break
            for feat in features:
                raw = dict(feat.get("attributes") or {})
                # Keep the source geometry alongside the attributes so a later
                # step can use it; we do not derive lat/lon here.
                if feat.get("geometry") is not None:
                    raw["geometry"] = feat["geometry"]
                yield raw
            if len(features) < PAGE_SIZE:
                break

    # -- normalize ------------------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)

        name = " ".join(
            part
            for part in (
                _clean(raw.get("lname")),
                _clean(raw.get("fname")),
                _clean(raw.get("mname")),
            )
            if part
        )

        dob = _from_epoch_ms(raw.get("birthday"))
        identity = Identity(
            full_name=name,
            sex=_sex(raw.get("gender")),
            dob=dob,
            year_of_birth=dob.year if dob else None,
        )

        # The layer publishes a full registration-address string plus an
        # область/район/город split. No offence text, statute, or tier is
        # served, so `offenses` stays empty rather than fabricating one.
        addresses = [
            Address(
                type="home",
                street=_clean(raw.get("address")),
                city=_clean(raw.get("regaddress2")),
                state=_clean(raw.get("regaddress")),
                country="KZ",
            )
        ]

        # globalid is a stable GUID; objectid is a reassignable sequence — prefer
        # the GUID as the per-person source key.
        source_id = str(
            raw.get("globalid") or raw.get("objectid") or ""
        ).strip()

        return OffenderRecord(
            source=Source(
                jurisdiction="KZ",
                source_id=source_id,
                source_url=EXPERIENCE,
                info_url=EXPERIENCE,
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            # status_public == 1 marks a record published to the public map.
            registration=Registration(
                status="active" if raw.get("status_public") == 1 else "unknown"
            ),
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        # Portraits are inlined as base64 in `raw["photo"]`; the source serves
        # no image URL, so there is nothing URL-shaped to return here.
        return []


def build() -> KzRegistryAdapter:
    return KzRegistryAdapter()
