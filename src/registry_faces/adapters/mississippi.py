"""Mississippi state registry adapter via NSOPW (browser-driven).

MS DPS runs the registry at `state.sor.dps.ms.gov`. The flow is:
  1. Click-through "Conditions of Use" page (ASP.NET ViewState form).
  2. After agreeing, the server redirects to `Captcha.aspx` — a CAPTCHA
     image challenge — before any search is allowed.

So the registry can't be scripted directly without solving the
captcha. This adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`. Same pattern as the other NSOPW
states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["MississippiAdapter", "ZIP_RANGE", "build"]

# Mississippi ZIPs live in 38601-39776. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(38600, 39800)


class MississippiAdapter(NsopwAdapter):
    jurisdiction = "US-MS"
    source_name = "NSOPW (Mississippi jurisdiction)"
    jurisdiction_code = "MS"
    zip_range = ZIP_RANGE
    run_log_subdir = "mississippi"


def build() -> MississippiAdapter:
    return MississippiAdapter()
