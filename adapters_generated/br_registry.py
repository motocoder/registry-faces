"""Brazil — Cadastro Estadual de Pedófilos (Mato Grosso do Sul / SEJUSP-MS).

Source type: public JSON API behind an AngularJS SPA.

Brazil has no single nationwide *public* offender registry that is
enumerable. The federal Cadastro Nacional de Pedófilos e Predadores Sexuais
(Lei 15.035/2024) is in force on paper but its public-consultation portal was
never launched — it remains "engavetado" (shelved) by the CNJ, so there is
nothing to enumerate there. Most other channels are police-only by law.

The one qualifying public, enumerable, government-run list is the
*Cadastro Estadual de Pedófilos* of Mato Grosso do Sul, created by State Law
5.038/2017 (amended by 6.067/2023) and published by the Secretaria de Estado
de Justiça e Segurança Pública (SEJUSP-MS) for open citizen consultation:

    https://portalservicos.sejusp.ms.gov.br/#!/consultapedofilos

The SPA is a thin shell over a public ASP.NET JSON endpoint that returns the
full grid in one call (no login, no CAPTCHA, no per-record gate):

    GET /api/Procurados/GetListaProcuradosPedofilia?quantidade=N
        -> {"return": [{id, nome, foto, sexo, ...}, ...], "message": null}

Each row carries name + a real, source-served thumbnail URL under
`/Content/Thumbnails/...`, so `extract_photos` returns that URL only. There is
no richer per-record detail endpoint; the grid row is the whole payload.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from registry_faces.schema import (
    Address,
    Identity,
    OffenderRecord,
    Registration,
    Source,
)
from web_scrubber.photos import PhotoRef
from registry_faces.adapters.base import Adapter

BASE = "https://portalservicos.sejusp.ms.gov.br"
LIST_ENDPOINT = f"{BASE}/api/Procurados/GetListaProcuradosPedofilia"
INFO_URL = f"{BASE}/#!/consultapedofilos"
USER_AGENT = "registry-faces/1.0 (+SEJUSP-MS Cadastro Estadual de Pedofilos)"

# The list endpoint returns everything up to `quantidade`; the registry holds a
# few hundred records. Ask for far more than the current size so we always get
# the full list in one request.
QUANTIDADE = 100000

_SEX = {"m": "M", "masculino": "M", "f": "F", "feminino": "F"}


def _sex(value: object) -> str:
    """Map SEJUSP's `sexo` (string, dict, or null) to the canonical code."""
    if isinstance(value, dict):
        value = value.get("descricao") or value.get("nome") or value.get("id")
    if value is None:
        return "unknown"
    return _SEX.get(str(value).strip().lower(), "unknown")


class BrRegistryAdapter(Adapter):
    jurisdiction = "BR-MS"
    source_name = "Cadastro Estadual de Pedofilos (SEJUSP-MS)"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=60.0,
            follow_redirects=True,
        )

    # -- fetch ----------------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        resp = self._client.get(LIST_ENDPOINT, params={"quantidade": QUANTIDADE})
        resp.raise_for_status()
        rows = (resp.json() or {}).get("return") or []
        # Server order varies between calls; sort by id for determinism.
        for row in sorted(rows, key=lambda r: r.get("id") or 0):
            yield row

    # -- normalize ------------------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        now = datetime.now(timezone.utc)

        identity = Identity(
            full_name=(raw.get("nome") or "").strip(),
            sex=_sex(raw.get("sexo")),
        )

        # Only the jurisdiction (MS, Brazil) is published per record.
        addresses = [Address(type="home", state="MS", country="BR")]

        return OffenderRecord(
            source=Source(
                jurisdiction="BR-MS",
                source_id=str(raw.get("id")),
                source_url=INFO_URL,
                info_url=INFO_URL,
                fetched_at=now,
            ),
            identity=identity,
            addresses=addresses,
            registration=Registration(status="active"),
            raw=raw,
        )

    # -- photos ---------------------------------------------------------------

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        foto = raw.get("foto")
        if not foto:
            return []
        return [
            PhotoRef(
                url=urljoin(BASE, foto),
                source_type="registry",
                source_name=self.source_name,
                source_jurisdiction=self.jurisdiction,
                source_id=str(raw.get("id")),
                caption=(raw.get("nome") or "").strip() or None,
            )
        ]


def build() -> BrRegistryAdapter:
    return BrRegistryAdapter()
