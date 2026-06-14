"""Wyoming state registry adapter via NSOPW (browser-driven).

Wyoming's registry is published via OffenderWatch
(`communitynotification.com?office=55699`), which sits behind
DataDome — same gate WA and UT hit. There is no WY-DCI-direct
registry website. So this adapter rides the shared NSOPW base
(`_nsopw.NsopwAdapter`), which drives a headless Chromium past
Cloudflare on `nsopw-api.ojp.gov`.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["WyomingAdapter", "ZIP_RANGE", "build"]

# Wyoming ZIPs live in 82001-83128. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(82000, 83200)


class WyomingAdapter(NsopwAdapter):
    jurisdiction = "US-WY"
    source_name = "NSOPW (Wyoming jurisdiction)"
    jurisdiction_code = "WY"
    zip_range = ZIP_RANGE
    run_log_subdir = "wyoming"


def build() -> WyomingAdapter:
    return WyomingAdapter()
