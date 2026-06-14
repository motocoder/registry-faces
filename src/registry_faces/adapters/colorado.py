"""Colorado state registry adapter via NSOPW (browser-driven).

CBI runs a JSF-based public registry at
`apps.colorado.gov/apps/dps/sor/index.jsf`, but it explicitly states
"This site does not display the entire list of registrants in
Colorado" — the complete list is only released by written request
to CBI with a fee under C.R.S. 16-22-111. So scraping the JSF would
not give complete coverage either.

NSOPW gets the same publicly-shareable subset Colorado publishes via
federation, with no acceptance gate to clear. Same pattern as
WA/UT/WY/AL/AK/AZ/AR.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["ColoradoAdapter", "ZIP_RANGE", "build"]

# Colorado ZIPs live in 80001-81658. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(80000, 81700)


class ColoradoAdapter(NsopwAdapter):
    jurisdiction = "US-CO"
    source_name = "NSOPW (Colorado jurisdiction)"
    jurisdiction_code = "CO"
    zip_range = ZIP_RANGE
    run_log_subdir = "colorado"


def build() -> ColoradoAdapter:
    return ColoradoAdapter()
