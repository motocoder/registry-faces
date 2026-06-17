"""West Virginia state registry adapter via NSOPW (browser-driven).

The West Virginia State Police run the registry. It federates into NSOPW,
so this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`), which
drives a headless Chromium past Cloudflare on `nsopw-api.ojp.gov`. Same
pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["WestVirginiaAdapter", "ZIP_RANGE", "build"]

# West Virginia ZIPs live in 24700-26886. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one on
# fallback.
ZIP_RANGE = range(24700, 26900)


class WestVirginiaAdapter(NsopwAdapter):
    jurisdiction = "US-WV"
    source_name = "NSOPW (West Virginia jurisdiction)"
    jurisdiction_code = "WV"
    zip_range = ZIP_RANGE
    run_log_subdir = "west_virginia"


def build() -> WestVirginiaAdapter:
    return WestVirginiaAdapter()
