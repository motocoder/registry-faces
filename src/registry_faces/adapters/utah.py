"""Utah state registry adapter via NSOPW (browser-driven).

Utah's registry is published via OffenderWatch
(`communitynotification.com?office=54438`), which sits behind
DataDome — same gate WA hits on icrimewatch.net. There is no
UDC-direct registry website. So this adapter rides the shared NSOPW
base (`_nsopw.NsopwAdapter`), which drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["UtahAdapter", "ZIP_RANGE", "build"]

# Utah ZIPs live in 840xx-847xx (approx 84001-84791). Sweep the full
# 5-digit range; invalid zips come back with statusCode 117 and are
# skipped one-by-one on fallback.
ZIP_RANGE = range(84000, 84800)


class UtahAdapter(NsopwAdapter):
    jurisdiction = "US-UT"
    source_name = "NSOPW (Utah jurisdiction)"
    jurisdiction_code = "UT"
    zip_range = ZIP_RANGE
    run_log_subdir = "utah"


def build() -> UtahAdapter:
    return UtahAdapter()
