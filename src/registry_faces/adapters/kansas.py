"""Kansas state registry adapter via NSOPW (browser-driven).

KBI runs the registry at `kbi.ks.gov/ro.shtml` (and several alias
URLs), but every direct request gets dropped by F5 BIG-IP into a
"my.logout.php3?errorcode=19" page citing "Access was denied by an
access control list." Different errorcodes appear for different paths,
all citing ACL denials. There's no DOC alternative, no API, no public
bulk download.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["KansasAdapter", "ZIP_RANGE", "build"]

# Kansas ZIPs live in 66002-67954. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(66000, 68000)


class KansasAdapter(NsopwAdapter):
    jurisdiction = "US-KS"
    source_name = "NSOPW (Kansas jurisdiction)"
    jurisdiction_code = "KS"
    zip_range = ZIP_RANGE
    run_log_subdir = "kansas"


def build() -> KansasAdapter:
    return KansasAdapter()
