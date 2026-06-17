"""Vermont state registry adapter via NSOPW (browser-driven).

Vermont's registry is run by the Vermont Crime Information Center. It
federates into NSOPW, so this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past Cloudflare
on `nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["VermontAdapter", "ZIP_RANGE", "build"]

# Vermont ZIPs live in 05001-05907. Sweep the full 5-digit range; invalid
# zips come back with statusCode 117 and are skipped one-by-one on fallback.
ZIP_RANGE = range(5000, 6000)


class VermontAdapter(NsopwAdapter):
    jurisdiction = "US-VT"
    source_name = "NSOPW (Vermont jurisdiction)"
    jurisdiction_code = "VT"
    zip_range = ZIP_RANGE
    run_log_subdir = "vermont"


def build() -> VermontAdapter:
    return VermontAdapter()
