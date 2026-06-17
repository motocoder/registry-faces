"""New Hampshire state registry adapter via NSOPW (browser-driven).

The NH State Police registry lives at `business.nh.gov/nsor/`, but
every `*.nh.gov` host returns 403 ("Access Denied") for non-allowlisted
clients regardless of browser-style headers — looks like an edge-level
ACL similar to Kansas's F5 setup. No DOC alternative is reachable.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["NewHampshireAdapter", "ZIP_RANGE", "build"]

# New Hampshire ZIPs live in 03031-03897. The base formats each integer
# as a zero-padded 5-digit string so `range(3000, 3900)` resolves to
# "03000".."03899" at query time. Invalid zips come back with statusCode
# 117 and are skipped one-by-one on fallback.
ZIP_RANGE = range(3000, 3900)


class NewHampshireAdapter(NsopwAdapter):
    jurisdiction = "US-NH"
    source_name = "NSOPW (New Hampshire jurisdiction)"
    jurisdiction_code = "NH"
    zip_range = ZIP_RANGE
    run_log_subdir = "new_hampshire"


def build() -> NewHampshireAdapter:
    return NewHampshireAdapter()
