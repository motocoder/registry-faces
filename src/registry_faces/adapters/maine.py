"""Maine state registry adapter via NSOPW (browser-driven).

Maine SBI runs the registry at `sor.informe.org/sor/` (Perl CGI at
`index.pl`). The cert has been expired since at least 2026-05.
Every search variant I tried — POST/GET, with/without an `accept`
checkbox, every plausible accept value (`on`, `Y`, `1`, `true`, etc.),
with/without a Referer header — silently returns the landing page
without results. There's no captcha or visible CSRF token; the
server-side gate isn't observable from outside.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["MaineAdapter", "ZIP_RANGE", "build"]

# Maine ZIPs live in 03901-04992. Sweep the full 5-digit range; the base
# formats each integer as a zero-padded 5-digit string so `range(3900,
# 5000)` resolves to "03900".."04999" at query time. Invalid zips come
# back with statusCode 117 and are skipped one-by-one on fallback.
ZIP_RANGE = range(3900, 5000)


class MaineAdapter(NsopwAdapter):
    jurisdiction = "US-ME"
    source_name = "NSOPW (Maine jurisdiction)"
    jurisdiction_code = "ME"
    zip_range = ZIP_RANGE
    run_log_subdir = "maine"


def build() -> MaineAdapter:
    return MaineAdapter()
