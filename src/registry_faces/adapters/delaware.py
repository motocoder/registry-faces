"""Delaware state registry adapter via NSOPW (browser-driven).

DSP runs a JSON-API-backed search at `sexoffender.dsp.delaware.gov/`
with discoverable endpoints (`/Search`, `/GetOffenderSummaries`,
`/GetOffenderDetails`, `/Image/Full`, `/Image/Thumb`), but every call
to `/Search` returns `{"success": false, "needsCaptcha": true}` — the
endpoint is gated by Google reCAPTCHA v2 and won't yield results
without a human-solved token.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["DelawareAdapter", "ZIP_RANGE", "build"]

# Delaware ZIPs live in 19701-19980. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(19700, 20000)


class DelawareAdapter(NsopwAdapter):
    jurisdiction = "US-DE"
    source_name = "NSOPW (Delaware jurisdiction)"
    jurisdiction_code = "DE"
    zip_range = ZIP_RANGE
    run_log_subdir = "delaware"


def build() -> DelawareAdapter:
    return DelawareAdapter()
