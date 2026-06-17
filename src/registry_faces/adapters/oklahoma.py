"""Oklahoma state registry adapter via NSOPW (browser-driven).

OK DOC links to its SOR at `sors.doc.ok.gov/ords/svorp/sors/r/sors/`
(Oracle APEX). Cloudflare in front of it blocks every non-allowlisted
client with a "Sorry, you have been blocked" page. There's no DOC-
hosted public bulk alternative.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["OklahomaAdapter", "ZIP_RANGE", "build"]

# Oklahoma ZIPs live in 73001-74966. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(73000, 75000)


class OklahomaAdapter(NsopwAdapter):
    jurisdiction = "US-OK"
    source_name = "NSOPW (Oklahoma jurisdiction)"
    jurisdiction_code = "OK"
    zip_range = ZIP_RANGE
    run_log_subdir = "oklahoma"


def build() -> OklahomaAdapter:
    return OklahomaAdapter()
