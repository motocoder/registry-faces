"""Nevada state registry adapter via NSOPW (browser-driven).

Nevada does not host a centralized public sex offender registry portal.
Per NRS 179B.270 only a restricted subset of registration data is
publicly accessible, and that subset is distributed to local agencies
(Clark/Las Vegas Metro, Washoe/Reno, etc.) rather than served from a
single state site. The DPS and AG sites both 404 on the obvious SOR
paths.

NSOPW federates whatever Nevada externally publishes — that's what
this adapter pulls. Same NSOPW-via-Playwright pattern as the other
states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["NevadaAdapter", "ZIP_RANGE", "build"]

# Nevada ZIPs live in 88901-89883. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(88900, 89900)


class NevadaAdapter(NsopwAdapter):
    jurisdiction = "US-NV"
    source_name = "NSOPW (Nevada jurisdiction)"
    jurisdiction_code = "NV"
    zip_range = ZIP_RANGE
    run_log_subdir = "nevada"


def build() -> NevadaAdapter:
    return NevadaAdapter()
