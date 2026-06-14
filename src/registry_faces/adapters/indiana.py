"""Indiana state registry adapter via NSOPW (browser-driven).

Indiana's Sex and Violent Offender Registry is run by the Indiana
Sheriffs' Association and exclusively served via OffenderWatch at
`icrimewatch.net/indiana.php` — the indianasheriffs.org SOR page and
the in.gov CJI page both link there, with no alternative path. The
icrimewatch host sits behind DataDome (HTTP 403 + JS challenge).

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["IndianaAdapter", "ZIP_RANGE", "build"]

# Indiana ZIPs live in 46001-47997. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(46000, 48000)


class IndianaAdapter(NsopwAdapter):
    jurisdiction = "US-IN"
    source_name = "NSOPW (Indiana jurisdiction)"
    jurisdiction_code = "IN"
    zip_range = ZIP_RANGE
    run_log_subdir = "indiana"


def build() -> IndianaAdapter:
    return IndianaAdapter()
