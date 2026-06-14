"""Kentucky state registry adapter via NSOPW (browser-driven).

KSP runs the registry at `http://kspsor.state.ky.us/`. Every search
form (Quick, Radius, Notify) posts back with a `ReCaptchaToken` field
and the page loads `google.com/recaptcha/api.js?render=...` — reCAPTCHA
v3, invisible. Direct API calls without a fresh token won't return
data.

So this adapter rides the shared NSOPW base (`_nsopw.NsopwAdapter`),
which drives a headless Chromium past Cloudflare on
`nsopw-api.ojp.gov`. Same pattern as the other NSOPW states.

Requires the `playwright` optional dependency and a one-time Chromium
install: `pip install 'registry-faces[wa]' && playwright install chromium`
(reuses the WA extra — same dependency set).
"""

from __future__ import annotations

from ._nsopw import NsopwAdapter

__all__ = ["KentuckyAdapter", "ZIP_RANGE", "build"]

# Kentucky ZIPs live in 40003-42788. Sweep the full 5-digit range;
# invalid zips come back with statusCode 117 and are skipped one-by-one
# on fallback.
ZIP_RANGE = range(40000, 42800)


class KentuckyAdapter(NsopwAdapter):
    jurisdiction = "US-KY"
    source_name = "NSOPW (Kentucky jurisdiction)"
    jurisdiction_code = "KY"
    zip_range = ZIP_RANGE
    run_log_subdir = "kentucky"


def build() -> KentuckyAdapter:
    return KentuckyAdapter()
