"""Arkansas state registry adapter via NSOPW (browser-driven).

ACIC links to two AR-direct interfaces:
  * `ark.org/offender-search/index.php` — a Vue/Angular SPA whose
    JS/CSS bundles all 404 to non-browser requests (Information Network
    of Arkansas WAF gates asset paths even with proper Referer / UA).
  * `sexoffenderregistry.ar.gov/le/#/tos` — LE-only, requires login.

No public AR-direct path is scriptable without a deeper browser session
than NSOPW needs. So this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which already drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`. Same pattern as WA/UT/WY/AL/AK/AZ.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["ArkansasAdapter", "ZIP_RANGE", "build"]

# Arkansas ZIPs live in 71601-72959. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(71600, 73000)


class ArkansasAdapter(NsopwAdapter):
    jurisdiction = "US-AR"
    source_name = "NSOPW (Arkansas jurisdiction)"
    jurisdiction_code = "AR"
    zip_range = ZIP_RANGE
    run_log_subdir = "arkansas"


def build() -> ArkansasAdapter:
    return ArkansasAdapter()
