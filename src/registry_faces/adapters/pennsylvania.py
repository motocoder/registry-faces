"""Pennsylvania state registry adapter via NSOPW (browser-driven).

PA State Police runs Megan's Law at `meganslaw.psp.pa.gov`. The flow:
  1. Terms-and-conditions click-through (ASP.NET MVC with anti-forgery
     token) — easy.
  2. Every `/Search/<*>Search` form submits a hidden `RecaptchaToken`
     field. The result page renders empty when the token is missing;
     this is reCAPTCHA v3 score-gated.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["PennsylvaniaAdapter", "ZIP_RANGE", "build"]

# Pennsylvania ZIPs live in 15001-19640. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(15000, 19700)


class PennsylvaniaAdapter(NsopwAdapter):
    jurisdiction = "US-PA"
    source_name = "NSOPW (Pennsylvania jurisdiction)"
    jurisdiction_code = "PA"
    zip_range = ZIP_RANGE
    run_log_subdir = "pennsylvania"


def build() -> PennsylvaniaAdapter:
    return PennsylvaniaAdapter()
