"""South Carolina state registry adapter via NSOPW (browser-driven).

SLED runs the registry at `scor.sled.sc.gov`. The flow is:
  1. POST `ConditionsOfUse.Aspx` with ViewState + Agree — easy.
  2. After agreeing, the server redirects to `Captcha.aspx` — a
     BotDetect image CAPTCHA — before any search is allowed.

So the registry can't be scripted directly without solving a fresh
CAPTCHA per session. This adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`. Same pattern as the other NSOPW
states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["SouthCarolinaAdapter", "ZIP_RANGE", "build"]

# South Carolina ZIPs live in 29001-29948. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(29000, 30000)


class SouthCarolinaAdapter(NsopwAdapter):
    jurisdiction = "US-SC"
    source_name = "NSOPW (South Carolina jurisdiction)"
    jurisdiction_code = "SC"
    zip_range = ZIP_RANGE
    run_log_subdir = "south_carolina"


def build() -> SouthCarolinaAdapter:
    return SouthCarolinaAdapter()
