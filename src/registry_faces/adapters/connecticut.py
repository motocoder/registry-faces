"""Connecticut state registry adapter via NSOPW (browser-driven).

CT DESPP links to OffenderWatch
(`communitynotification.com?office=54567`) as the official SOR portal,
which sits behind DataDome. The legacy state.ct.us page is also gated
by a CAPTCHA. There is no CT-direct registry interface that's
scriptable without solving a gate.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as WA/UT/WY/AL/AK/AZ/AR/CO.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["ConnecticutAdapter", "ZIP_RANGE", "build"]

# Connecticut ZIPs live in 06001-06928. The base formats each integer
# as a zero-padded 5-digit string so `range(6000, 7000)` resolves to
# "06000".."06999" at query time. Invalid zips come back with statusCode
# 117 and are skipped one-by-one on fallback.
ZIP_RANGE = range(6000, 7000)


class ConnecticutAdapter(NsopwAdapter):
    jurisdiction = "US-CT"
    source_name = "NSOPW (Connecticut jurisdiction)"
    jurisdiction_code = "CT"
    zip_range = ZIP_RANGE
    run_log_subdir = "connecticut"


def build() -> ConnecticutAdapter:
    return ConnecticutAdapter()
