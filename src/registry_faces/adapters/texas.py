"""Texas state registry adapter via NSOPW (browser-driven).

Texas DPS runs its own registry at `records.txdps.state.tx.us`, but it
sits behind a session/ViewState flow that resists direct scripting. Texas
does federate into NSOPW, so this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past Cloudflare
on `nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["TexasAdapter", "ZIP_RANGE", "build"]

# Texas ZIPs live in 75001-79999 (the El Paso 885xx cluster is shared with
# New Mexico and caught by the pass-2 name sweep). Sweep the full 5-digit
# range; invalid zips come back with statusCode 117 and are skipped
# one-by-one on fallback.
ZIP_RANGE = range(75000, 80000)


class TexasAdapter(NsopwAdapter):
    jurisdiction = "US-TX"
    source_name = "NSOPW (Texas jurisdiction)"
    jurisdiction_code = "TX"
    zip_range = ZIP_RANGE
    run_log_subdir = "texas"


def build() -> TexasAdapter:
    return TexasAdapter()
