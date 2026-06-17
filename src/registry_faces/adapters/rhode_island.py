"""Rhode Island state registry adapter via NSOPW (browser-driven).

RISP's Sex Offenders page at `risp.ri.gov/safety-education/sex-offenders`
links its "RISOR public website" button to OffenderWatch via
`sheriffalerts.com/cap_main.php?office=56404`, which sits behind
DataDome. There's no RISP-direct registry interface.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["RhodeIslandAdapter", "ZIP_RANGE", "build"]

# Rhode Island ZIPs live in 02801-02940. The base formats each integer
# as a zero-padded 5-digit string so `range(2800, 2950)` resolves to
# "02800".."02949" at query time. Invalid zips come back with statusCode
# 117 and are skipped one-by-one on fallback.
ZIP_RANGE = range(2800, 2950)


class RhodeIslandAdapter(NsopwAdapter):
    jurisdiction = "US-RI"
    source_name = "NSOPW (Rhode Island jurisdiction)"
    jurisdiction_code = "RI"
    zip_range = ZIP_RANGE
    run_log_subdir = "rhode_island"


def build() -> RhodeIslandAdapter:
    return RhodeIslandAdapter()
