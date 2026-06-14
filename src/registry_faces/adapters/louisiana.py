"""Louisiana state registry adapter via NSOPW (browser-driven).

LSP's Community Outreach SOR page links its "I AGREE" button directly
to `icrimewatch.net/louisiana.php`, which is the OffenderWatch portal
behind DataDome. No LSP-direct registry interface exists, and there's
no public bulk download.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["LouisianaAdapter", "ZIP_RANGE", "build"]

# Louisiana ZIPs live in 70001-71497. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(70000, 71500)


class LouisianaAdapter(NsopwAdapter):
    jurisdiction = "US-LA"
    source_name = "NSOPW (Louisiana jurisdiction)"
    jurisdiction_code = "LA"
    zip_range = ZIP_RANGE
    run_log_subdir = "louisiana"


def build() -> LouisianaAdapter:
    return LouisianaAdapter()
