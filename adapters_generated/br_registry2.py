"""Brazil – Mato Grosso do Sul state sex-offender registry adapter.

Operated by SEJUSP/MS (Secretaria de Estado de Justiça e Segurança Pública de
Mato Grosso do Sul) under State Law 5,038/2017 amended by Law 6,067/2023.

The public-tier API returns name + photo for all registered sex offenders;
offense details and personal identifiers beyond name are not published at this
tier. The full count is ~436 records (as of July 2026) and the API is
fully enumerable with a single call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import httpx

from registry_faces.adapters.base import Adapter
from registry_faces.photos import PhotoRef
from registry_faces.schema import Identity, Offense, OffenderRecord, Registration, Source

_BASE = "https://portalservicos.sejusp.ms.gov.br"
_LIST_URL = f"{_BASE}/api/Procurados/GetListaProcuradosPedofilia?quantidade=5000"
_PORTAL = f"{_BASE}/#/consultapedofilos"


class BrMsRegistryAdapter(Adapter):
    jurisdiction = "BR-MS"
    source_name = "Banco Estadual de Pedófilos - SEJUSP/MS"

    def fetch(self) -> Iterator[dict]:
        with httpx.Client(timeout=60) as client:
            resp = client.get(_LIST_URL)
            resp.raise_for_status()
            data = resp.json()
        for record in data.get("return") or []:
            yield record

    def normalize(self, raw: dict) -> OffenderRecord:
        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=str(raw["id"]),
                info_url=_PORTAL,
                fetched_at=datetime.now(tz=timezone.utc),
            ),
            identity=Identity(
                full_name=raw["nome"],
            ),
            offenses=[
                Offense(
                    raw_description=(
                        "Crime sexual (Banco Estadual de Pedófilos – SEJUSP/MS, "
                        "Lei Estadual 5.038/2017)"
                    ),
                )
            ],
            registration=Registration(status="active"),
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        foto = raw.get("foto")
        if not foto:
            return []
        url = (_BASE + foto) if foto.startswith("/") else foto
        return [PhotoRef(url=url)]


def build() -> BrMsRegistryAdapter:
    return BrMsRegistryAdapter()
