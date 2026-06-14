"""Illinois state registry adapter via NSOPW (browser-driven).

ISP runs an Angular SPA at `sor.isp.illinois.gov/sorpublic/`. The
bundle loads Google reCAPTCHA (`google.com/recaptcha/api.js`) and
tree-shakes its URL strings into runtime-resolved placeholders
(`host:"host"`, `host:"localhost"`) so the API endpoints aren't
discoverable from the static JS. Without a human-solved reCAPTCHA
token the search calls won't return data.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["IllinoisAdapter", "ZIP_RANGE", "build"]

# Illinois ZIPs live in 60001-62999. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(60000, 63000)


class IllinoisAdapter(NsopwAdapter):
    jurisdiction = "US-IL"
    source_name = "NSOPW (Illinois jurisdiction)"
    jurisdiction_code = "IL"
    zip_range = ZIP_RANGE
    run_log_subdir = "illinois"


def build() -> IllinoisAdapter:
    return IllinoisAdapter()
