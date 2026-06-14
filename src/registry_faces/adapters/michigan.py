"""Michigan state registry adapter via NSOPW (browser-driven).

MSP publishes the registry at `mspsor.com` (linked from
`michigan.gov/msp/services/sex-offender-registry`). The site loads
Google reCAPTCHA v3 (`api.js?render=...`) and gates every search
endpoint behind a valid token. There is no public bulk download or
unprotected API.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["MichiganAdapter", "ZIP_RANGE", "build"]

# Michigan ZIPs live in 48001-49971. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(48000, 50000)


class MichiganAdapter(NsopwAdapter):
    jurisdiction = "US-MI"
    source_name = "NSOPW (Michigan jurisdiction)"
    jurisdiction_code = "MI"
    zip_range = ZIP_RANGE
    run_log_subdir = "michigan"


def build() -> MichiganAdapter:
    return MichiganAdapter()
