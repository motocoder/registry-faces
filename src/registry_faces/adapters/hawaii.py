"""Hawaii state registry adapter via NSOPW (browser-driven).

The Hawaii direct paths are both gated:
  * `hcjdc.ehawaii.gov/bulkcor/` — requires HCJDC SSO login. Not a
    public bulk download.
  * `sexoffenders.ehawaii.gov/coveredoffender/` — public-facing search,
    but Google reCAPTCHA on every query.

NSOPW federates whatever HCJDC publishes externally, with no
acceptance gate on the federation surface. So this adapter rides the
shared NSOPW base (`_nsopw.NsopwAdapter`), which drives a headless
Chromium past Cloudflare on `nsopw-api.ojp.gov`. Same pattern as the
other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["HawaiiAdapter", "ZIP_RANGE", "build"]

# Hawaii ZIPs live in 96701-96898. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(96700, 96900)


class HawaiiAdapter(NsopwAdapter):
    jurisdiction = "US-HI"
    source_name = "NSOPW (Hawaii jurisdiction)"
    jurisdiction_code = "HI"
    zip_range = ZIP_RANGE
    run_log_subdir = "hawaii"


def build() -> HawaiiAdapter:
    return HawaiiAdapter()
