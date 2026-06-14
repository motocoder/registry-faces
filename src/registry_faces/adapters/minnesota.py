"""Minnesota state registry adapter via NSOPW (browser-driven).

Minnesota uniquely publishes only Level 3 (high-risk) predatory
offenders publicly — the full state predatory offender registry is
not searchable by the general public per Minn. Stat. § 244.052. The
DOC's public site at `mn.gov/doc/` sits behind ShieldSquare bot
protection that redirects every non-allowlisted client to
`validate.perfdrive.com`, so even the limited Level 3 listings aren't
scriptable directly.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. NSOPW federates whatever Minnesota DOC publishes
externally, which in practice is the Level 3 subset — expect a few
hundred records, not the full registrant population.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["MinnesotaAdapter", "ZIP_RANGE", "build"]

# Minnesota ZIPs live in 55001-56763. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(55000, 57000)


class MinnesotaAdapter(NsopwAdapter):
    jurisdiction = "US-MN"
    source_name = "NSOPW (Minnesota jurisdiction)"
    jurisdiction_code = "MN"
    zip_range = ZIP_RANGE
    run_log_subdir = "minnesota"


def build() -> MinnesotaAdapter:
    return MinnesotaAdapter()
