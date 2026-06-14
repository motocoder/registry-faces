"""Arizona state registry adapter via NSOPW (browser-driven).

AZDPS publishes a direct CSV link on
`azdps.gov/services/public-services-center/sex-offender-compliance`
(`http://icrimewatch.net/az_offenders.csv`), but the icrimewatch host
sits behind DataDome — every direct request returns 403 with a JS
challenge. There's no AZDPS-direct registry website.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as WA/UT/WY/AL/AK.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["ArizonaAdapter", "ZIP_RANGE", "build"]

# Arizona ZIPs live in 85001-86556. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(85000, 86600)


class ArizonaAdapter(NsopwAdapter):
    jurisdiction = "US-AZ"
    source_name = "NSOPW (Arizona jurisdiction)"
    jurisdiction_code = "AZ"
    zip_range = ZIP_RANGE
    run_log_subdir = "arizona"


def build() -> ArizonaAdapter:
    return ArizonaAdapter()
