"""Ohio state registry adapter via NSOPW (browser-driven).

The Ohio AG runs the eSORN (electronic Sex Offender Registration and
Notification) system at `esorn.ag.state.oh.us`. The host resolves
(CNAME chain to `esorn.net` at 208.75.1.199) but the TCP port is
firewalled to non-Ohio source IPs — ConnectTimeout on every probe,
including via Playwright. There is no AG-hosted public alternative.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["OhioAdapter", "ZIP_RANGE", "build"]

# Ohio ZIPs live in 43001-45999. Sweep the full 5-digit range; invalid
# zips come back with statusCode 117 and are skipped one-by-one on
# fallback.
ZIP_RANGE = range(43000, 46000)


class OhioAdapter(NsopwAdapter):
    jurisdiction = "US-OH"
    source_name = "NSOPW (Ohio jurisdiction)"
    jurisdiction_code = "OH"
    zip_range = ZIP_RANGE
    run_log_subdir = "ohio"


def build() -> OhioAdapter:
    return OhioAdapter()
