"""Puerto Rico sex offender registry adapter via NSOPW (browser-driven).

Puerto Rico's own public portal, the Registro de Ofensores Sexuales
(`sor.cjis.pr.gov` / `sor.pr.gov`, Policía de Puerto Rico / CJIS under
Law No. 266 of 2004), is a React SPA whose `/sort-api/offenders/*` search
endpoints are gated by Google reCAPTCHA: every call returns HTTP 403
("Forbidden") unless it carries an Authorization token minted from a
solved reCAPTCHA challenge. That is a CAPTCHA wall — not enumerable
without solving a challenge — so the direct portal is not a usable
adapter surface.

Puerto Rico is, however, a participating jurisdiction in NSOPW (it
resumed federating its registry to nsopw.gov in October 2017). NSOPW
re-publishes PR's records on the same federated search API every state
adapter in this repo uses, with no acceptance gate on the federation
surface. So this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`, scoped to the `PR` jurisdiction —
exactly the same pattern as Hawaii and the 50 states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from registry_faces.adapters._nsopw import NsopwAdapter

__all__ = ["PuertoRicoRegistryAdapter", "ZIP_RANGE", "build"]

# Puerto Rico ZIPs span 00601 (Adjuntas) to 00988 (Carolina): 006xx in the
# northwest, 007xx in the southeast, 009xx in the San Juan metro area. Sweep
# the full 006xx-009xx span; invalid zips come back with statusCode 117 and
# are skipped one-by-one on fallback.
ZIP_RANGE = range(600, 1000)


class PuertoRicoRegistryAdapter(NsopwAdapter):
    jurisdiction = "PR"
    source_name = "NSOPW (Puerto Rico jurisdiction)"
    jurisdiction_code = "PR"
    zip_range = ZIP_RANGE
    run_log_subdir = "puerto_rico"


def build() -> PuertoRicoRegistryAdapter:
    return PuertoRicoRegistryAdapter()
