"""Virginia state registry adapter via NSOPW (browser-driven).

Virginia State Police run the registry at `sex-offender.vsp.virginia.gov`,
behind a click-through agreement and per-session controls. Virginia
federates into NSOPW, so this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past Cloudflare
on `nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["VirginiaAdapter", "ZIP_RANGE", "build"]

# Virginia ZIPs live in 22000-24699 (the Northern Virginia 201xx cluster
# near DC is caught by the pass-2 name sweep). Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one on
# fallback.
ZIP_RANGE = range(22000, 24700)


class VirginiaAdapter(NsopwAdapter):
    jurisdiction = "US-VA"
    source_name = "NSOPW (Virginia jurisdiction)"
    jurisdiction_code = "VA"
    zip_range = ZIP_RANGE
    run_log_subdir = "virginia"


def build() -> VirginiaAdapter:
    return VirginiaAdapter()
