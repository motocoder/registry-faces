"""Washington state registry adapter via NSOPW (browser-driven).

Washington has no scriptable open endpoint of its own — every direct
path is gated:
  * `wasor.org` — WASPC's central registry. HTTP 301 to `127.0.0.1`.
    Effectively offline.
  * `icrimewatch.net?AgencyID=54528` — OffenderWatch portal. HTTP 403
    + DataDome challenge.
  * `nsopw-api.ojp.gov` — federated search backend. HTTP 403 +
    Cloudflare managed challenge. Reachable through a real browser.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare. Coverage notes:

  * NSOPW federates whatever WA publishes. WA publishes Level II,
    Level III, and non-compliant Level I — about 30% of registrants.
    Compliant Level I is not legally public and is not reachable here.

Public symbols (`NAME_LETTERS`, `ZIP_RANGE`, `BATCH_SIZE`,
`WashingtonAdapter`) are re-exported so `scripts/wa_resume.py` keeps
working without modification.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`.
"""

from __future__ import annotations

from ._nsopw import BATCH_SIZE, NAME_LETTERS, NsopwAdapter

__all__ = [
    "BATCH_SIZE",
    "NAME_LETTERS",
    "WashingtonAdapter",
    "ZIP_RANGE",
    "build",
]

# WA ZIPs live in 980xx-994xx. Sweep the full 5-digit range; invalid
# zips come back with statusCode 117 and are skipped one-by-one on
# fallback. Re-exported for `scripts/wa_resume.py`.
ZIP_RANGE = range(98000, 99500)


class WashingtonAdapter(NsopwAdapter):
    jurisdiction = "US-WA"
    source_name = "NSOPW (Washington jurisdiction)"
    jurisdiction_code = "WA"
    zip_range = ZIP_RANGE
    run_log_subdir = "washington"


def build() -> WashingtonAdapter:
    return WashingtonAdapter()
