"""Brazil (Mato Grosso do Sul) public pedophile registry adapter.

Source type: public JSON API behind a JS-rendered government portal.

Official public surface:
  https://portalservicos.sejusp.ms.gov.br/#/consultapedofilos

The portal is operated by the Mato Grosso do Sul Secretaria de Estado de
Justica e Seguranca Publica (SEJUSP-MS). Its Angular frontend calls a public
JSON endpoint:

  /api/Procurados/GetListaProcuradosPedofilia?quantidade=N

That endpoint returns an enumerable list of registry entries with a stable
numeric id, published name, and optional source-served thumbnail path. The
returned order is not stable across calls, so the adapter deduplicates by id
and sorts before yielding to keep ingest deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from registry_faces.schema import OffenderRecord, Source, Identity, Address, Offense
from registry_faces.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://portalservicos.sejusp.ms.gov.br"
LISTING_URL = f"{BASE}/#/consultapedofilos"
API_URL = f"{BASE}/api/Procurados/GetListaProcuradosPedofilia"
USER_AGENT = "registry-faces/1.0 (+public SEJUSP-MS pedophile registry index)"
MAX_QUANTITY = 1000

_SEX = {
    "MASCULINO": "M",
    "FEMININO": "F",
}


def _sex(value: str | None) -> str:
    return _SEX.get((value or "").strip().upper(), "unknown")


def _sort_key(raw: dict) -> tuple[int, str]:
    source_id = raw.get("id")
    try:
        return (int(source_id), "")
    except (TypeError, ValueError):
        return (0, str(source_id or ""))


class BrRegistryAdapter(Adapter):
    jurisdiction = "BR-MS"
    source_name = "SEJUSP-MS Cadastro Estadual de Pedofilos"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
        )

    def fetch(self) -> Iterator[dict]:
        resp = self._client.get(API_URL, params={"quantidade": MAX_QUANTITY})
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("return") or []

        deduped: dict[str, dict] = {}
        for row in rows:
            source_id = str(row.get("id") or "").strip()
            if not source_id:
                continue
            deduped[source_id] = row

        for raw in sorted(deduped.values(), key=_sort_key):
            yield raw

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)
        full_name = (raw.get("nome") or "").strip()

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=str(raw.get("id") or ""),
                source_url=LISTING_URL,
                info_url=LISTING_URL,
                fetched_at=now,
            ),
            identity=Identity(
                full_name=full_name,
                sex=_sex(raw.get("sexo")),
            ),
            addresses=[
                Address(
                    type="home",
                    state="Mato Grosso do Sul",
                    country="BR",
                )
            ],
            offenses=[
                Offense(
                    raw_description="Cadastro Estadual de Pedofilos",
                    jurisdiction=self.jurisdiction,
                )
            ],
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        photo_path = raw.get("foto")
        if not photo_path:
            return []
        return [
            PhotoRef(
                url=urljoin(BASE, photo_path),
                source_name=self.source_name,
                source_jurisdiction=self.jurisdiction,
                source_id=str(raw.get("id") or ""),
                caption=(raw.get("nome") or "").strip() or None,
            )
        ]


def build() -> BrRegistryAdapter:
    return BrRegistryAdapter()
