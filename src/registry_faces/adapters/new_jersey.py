"""New Jersey state registry adapter via NSOPW (browser-driven).

NJSP / Megan's Law publishes its registry through OffenderWatch
(`sheriffalerts.com -> communitynotification.com?office=55260`), which
sits behind DataDome. No NJSP-direct registry interface is reachable.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["NewJerseyAdapter", "ZIP_RANGE", "build"]

# New Jersey ZIPs live in 07001-08989. The base formats each integer as
# a zero-padded 5-digit string so `range(7000, 9000)` resolves to
# "07000".."08999" at query time. Invalid zips come back with statusCode
# 117 and are skipped one-by-one on fallback.
ZIP_RANGE = range(7000, 9000)


class NewJerseyAdapter(NsopwAdapter):
    jurisdiction = "US-NJ"
    source_name = "NSOPW (New Jersey jurisdiction)"
    jurisdiction_code = "NJ"
    zip_range = ZIP_RANGE
    run_log_subdir = "new_jersey"


def build() -> NewJerseyAdapter:
    return NewJerseyAdapter()
