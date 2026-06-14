"""Alaska state registry adapter via NSOPW (browser-driven).

Alaska's DPS registry at `sor.dps.alaska.gov/sorpublic/` is a Cloudflare-
gated Angular SPA. Every call (including the SPA's own config bootstrap
at `/sorpublic/build?app=cpi-ng&path=config`) requires real browser
fingerprints — the same gate WA/UT/WY/AL hit on NSOPW.

Going direct means driving the SPA via Playwright AND reverse-engineering
its config-driven endpoints, which is strictly more work than routing
through NSOPW. So this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which already drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["AlaskaAdapter", "ZIP_RANGE", "build"]

# Alaska ZIPs live in 99500-99999. Sweep the full 5-digit range; invalid
# zips come back with statusCode 117 and are skipped one-by-one on
# fallback.
ZIP_RANGE = range(99500, 100000)


class AlaskaAdapter(NsopwAdapter):
    jurisdiction = "US-AK"
    source_name = "NSOPW (Alaska jurisdiction)"
    jurisdiction_code = "AK"
    zip_range = ZIP_RANGE
    run_log_subdir = "alaska"


def build() -> AlaskaAdapter:
    return AlaskaAdapter()
