"""Maryland state registry adapter via NSOPW (browser-driven).

DPSCS publishes its Sex Offender Registry at
`dpscs.maryland.gov/onlineservs/socem/default.shtml` but the actual
search button redirects through sheriffalerts.com to OffenderWatch
(`communitynotification.com?office=56622`), which sits behind
DataDome. There is no DPSCS-direct registry interface.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["MarylandAdapter", "ZIP_RANGE", "build"]

# Maryland ZIPs live in 20601-21930. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(20600, 22000)


class MarylandAdapter(NsopwAdapter):
    jurisdiction = "US-MD"
    source_name = "NSOPW (Maryland jurisdiction)"
    jurisdiction_code = "MD"
    zip_range = ZIP_RANGE
    run_log_subdir = "maryland"


def build() -> MarylandAdapter:
    return MarylandAdapter()
