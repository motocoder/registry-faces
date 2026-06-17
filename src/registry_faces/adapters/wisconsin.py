"""Wisconsin state registry adapter via NSOPW (browser-driven).

The Wisconsin DOC runs the registry at `appsdoc.wi.gov/public`. It
federates into NSOPW, so this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past Cloudflare
on `nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["WisconsinAdapter", "ZIP_RANGE", "build"]

# Wisconsin ZIPs live in 53001-54990. Sweep the full 5-digit range; invalid
# zips come back with statusCode 117 and are skipped one-by-one on fallback.
ZIP_RANGE = range(53000, 55000)


class WisconsinAdapter(NsopwAdapter):
    jurisdiction = "US-WI"
    source_name = "NSOPW (Wisconsin jurisdiction)"
    jurisdiction_code = "WI"
    zip_range = ZIP_RANGE
    run_log_subdir = "wisconsin"


def build() -> WisconsinAdapter:
    return WisconsinAdapter()
