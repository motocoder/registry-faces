"""Alabama state registry adapter via NSOPW (browser-driven).

Alabama's "Sex Offender Search" on ALEA's Community Information Center
(`app.alea.gov/Community/`) just redirects users to OffenderWatch
(`communitynotification.com?office=54247`), which sits behind
DataDome — same gate WA, UT, and WY hit. ALEA's in-house wfSearch.aspx
handles Missing Persons, Amber/Blue Alerts, and Fugitives, but not
sex offenders. So this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["AlabamaAdapter", "ZIP_RANGE", "build"]

# Alabama ZIPs live in 35004-36925. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(35000, 37000)


class AlabamaAdapter(NsopwAdapter):
    jurisdiction = "US-AL"
    source_name = "NSOPW (Alabama jurisdiction)"
    jurisdiction_code = "AL"
    zip_range = ZIP_RANGE
    run_log_subdir = "alabama"


def build() -> AlabamaAdapter:
    return AlabamaAdapter()
