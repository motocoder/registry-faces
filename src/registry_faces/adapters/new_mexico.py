"""New Mexico state registry adapter via NSOPW (browser-driven).

NMDPS routes its Sex Offender Registry link through sheriffalerts.com
to OffenderWatch (`communitynotification.com?office=55290`), which
sits behind DataDome. There's no NMDPS-direct registry interface.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["NewMexicoAdapter", "ZIP_RANGE", "build"]

# New Mexico ZIPs live in 87001-88439. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(87000, 88500)


class NewMexicoAdapter(NsopwAdapter):
    jurisdiction = "US-NM"
    source_name = "NSOPW (New Mexico jurisdiction)"
    jurisdiction_code = "NM"
    zip_range = ZIP_RANGE
    run_log_subdir = "new_mexico"


def build() -> NewMexicoAdapter:
    return NewMexicoAdapter()
