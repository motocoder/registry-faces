"""Shared base for NSOPW-via-Playwright state adapters.

NSOPW (`nsopw-api.ojp.gov`) is the federal aggregator that re-publishes
state registry data. The HTTP endpoint sits behind a Cloudflare managed
challenge that only real browsers can pass; this base drives a headless
Chromium via Playwright to navigate the site once (Cloudflare issues
cookies), then calls the JSON search API from inside the page's JS
context.

State adapters that build on this base set:

    class FooAdapter(NsopwAdapter):
        jurisdiction = "US-XX"
        source_name = "NSOPW (Foo jurisdiction)"
        jurisdiction_code = "XX"          # NSOPW's 2-letter code
        zip_range = range(80000, 80999)   # state's published ZIP span
        run_log_subdir = "foo"            # under registry-runs/

All call shapes, rate-limiting, failure backoff, and result handling
are inherited as-is. The base also defines `normalize()` and
`extract_photos()` — NSOPW returns the same payload shape regardless of
which state's records came back.

Enumeration is two passes:
  1. ZIP sweep over `zip_range`, batched 5 ZIPs per call. Catches
     anyone with an in-state address.
  2. Name-prefix sweep across 26x26 letter pairs, scoped to
     `jurisdictions: [jurisdiction_code]`. Catches absconders, transients,
     and records with no ZIP. On `statusCode: 511` ("too many results
     for one letter pair") the surname prefix is extended by one
     letter and recursed.

Records are deduped by `offenderUri` across both passes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..photos import PhotoRef
from ..schema import Address, Identity, OffenderRecord, Registration, Source
from .base import Adapter

API_URL = "https://nsopw-api.ojp.gov/nsopw/v1/v1.0/search"
HOMEPAGE = "https://www.nsopw.gov/search-public-sex-offender-registries"
BATCH_SIZE = 5
NAME_LETTERS = "abcdefghijklmnopqrstuvwxyz"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class NsopwAdapter(Adapter):
    # ---- subclass contract --------------------------------------------

    jurisdiction_code: str = ""
    """NSOPW's two-letter jurisdiction code, e.g. "WA", "UT", "WY", "AL"."""

    zip_range: range = range(0, 0)
    """The state's full 5-digit ZIP range, e.g. range(98000, 99500)."""

    run_log_subdir: str = "nsopw"
    """Subdirectory under registry-runs/ where this state's run logs go."""

    # ---- init ---------------------------------------------------------

    def __init__(
        self,
        headless: bool = True,
        batch_size: int = BATCH_SIZE,
        request_delay_s: float = 6.0,
        failure_backoff_s: float = 15.0,
        renav_after_consecutive_failures: int = 4,
        progress_every: int = 25,
        name_sweep: bool = True,
        run_log_dir: Path | str | None = None,
        queries: list[dict] | None = None,
    ) -> None:
        if not self.jurisdiction_code or not self.zip_range:
            raise NotImplementedError(
                f"{type(self).__name__} must set jurisdiction_code and zip_range"
            )
        self.headless = headless
        self.batch_size = batch_size
        # 5-6s is the sustainable per-call rate against NSOPW behind
        # Cloudflare. Probes showed 3s → 70% success, 5s → 80%, 8s → 100%.
        # 6s + per-failure backoff gives a good speed/reliability balance.
        self.request_delay_s = request_delay_s
        self.failure_backoff_s = failure_backoff_s
        self.renav_after_consecutive_failures = renav_after_consecutive_failures
        self.progress_every = progress_every
        self.name_sweep = name_sweep
        self.queries = queries
        if run_log_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_log_dir = Path("registry-runs") / self.run_log_subdir / stamp
        self.run_log_dir = Path(run_log_dir)
        self._fetched_at: datetime | None = None
        self._success_f = None
        self._failed_f = None
        self._n_success = 0
        self._n_failed = 0
        self._consecutive_failures = 0
        self._page = None

    # ---- fetch --------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                f"{type(self).__name__} needs playwright. "
                "Install: pip install 'registry-faces[wa]' "
                "&& playwright install chromium"
            ) from e

        self._fetched_at = datetime.now(timezone.utc)
        code = self.jurisdiction_code
        zips = [f"{i:05d}" for i in self.zip_range]
        seen: set[str] = set()
        batches_done = 0
        zips_skipped = 0
        records_yielded = 0

        self.run_log_dir.mkdir(parents=True, exist_ok=True)
        success_path = self.run_log_dir / "successful.jsonl"
        failed_path = self.run_log_dir / "failed.jsonl"
        print(f"  {code} logs: {self.run_log_dir} (successful.jsonl, failed.jsonl)", flush=True)

        with (
            open(success_path, "a", buffering=1) as self._success_f,
            open(failed_path, "a", buffering=1) as self._failed_f,
            sync_playwright() as p,
        ):
            browser = p.chromium.launch(headless=self.headless)
            try:
                ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
                page = ctx.new_page()
                self._page = page
                page.goto(HOMEPAGE, wait_until="networkidle", timeout=60_000)
                page.wait_for_timeout(3000)

                if self.queries is not None:
                    print(
                        f"  {code} resume: running {len(self.queries)} custom queries",
                        flush=True,
                    )
                    done = 0
                    for query in self.queries:
                        resp = self._post_search(page, query)
                        if resp is not None and resp.get("statusCode") in (200, 201):
                            for off in resp.get("offenders") or []:
                                if off.get("jurisdictionId") != code:
                                    continue
                                uri = off.get("offenderUri") or _stable_id(off)
                                if uri in seen:
                                    continue
                                seen.add(uri)
                                records_yielded += 1
                                yield off
                        done += 1
                        if done % self.progress_every == 0:
                            print(
                                f"    resume queries={done}/{len(self.queries)}"
                                f"  yielded={records_yielded}"
                                f"  ok={self._n_success} fail={self._n_failed}",
                                flush=True,
                            )
                        time.sleep(self.request_delay_s)
                    print(
                        f"  {code} resume done: {records_yielded} new records. "
                        f"queries: {self._n_success} succeeded, {self._n_failed} failed. "
                        f"Logs: {self.run_log_dir}",
                        flush=True,
                    )
                    return

                print(
                    f"  {code} pass 1/2: zip sweep "
                    f"({len(zips)} zips, batch={self.batch_size})",
                    flush=True,
                )
                for batch in _chunked(zips, self.batch_size):
                    result = self._search_zips(page, batch)
                    if result is None:
                        zips_skipped += len(batch)
                    else:
                        offenders, invalid_count = result
                        zips_skipped += invalid_count
                        for off in offenders:
                            if off.get("jurisdictionId") != code:
                                continue
                            uri = off.get("offenderUri") or _stable_id(off)
                            if uri in seen:
                                continue
                            seen.add(uri)
                            records_yielded += 1
                            yield off
                    batches_done += 1
                    if batches_done % self.progress_every == 0:
                        print(
                            f"    zip batches={batches_done}/{len(zips)//self.batch_size}"
                            f"  yielded={records_yielded}  invalid_zips={zips_skipped}"
                            f"  ok={self._n_success} fail={self._n_failed}",
                            flush=True,
                        )
                    time.sleep(self.request_delay_s)

                zip_yield = records_yielded
                if self.name_sweep:
                    print(
                        f"  {code} pass 2/2: name-prefix sweep "
                        f"({len(NAME_LETTERS)**2} pairs) "
                        f"— catches absconders/transients missed by zip sweep",
                        flush=True,
                    )
                    pairs_done = 0
                    new_from_names = 0
                    for first_letter in NAME_LETTERS:
                        for last_letter in NAME_LETTERS:
                            offenders = self._search_name_prefix(page, first_letter, last_letter)
                            for off in offenders:
                                if off.get("jurisdictionId") != code:
                                    continue
                                uri = off.get("offenderUri") or _stable_id(off)
                                if uri in seen:
                                    continue
                                seen.add(uri)
                                records_yielded += 1
                                new_from_names += 1
                                yield off
                            pairs_done += 1
                            if pairs_done % self.progress_every == 0:
                                print(
                                    f"    name pairs={pairs_done}/{len(NAME_LETTERS)**2}"
                                    f"  total_yielded={records_yielded}"
                                    f"  new_from_names={new_from_names}"
                                    f"  ok={self._n_success} fail={self._n_failed}",
                                    flush=True,
                                )
                            time.sleep(self.request_delay_s)
                    print(
                        f"  {code} done: {records_yielded} total "
                        f"({zip_yield} from zips, {new_from_names} new from name sweep)",
                        flush=True,
                    )
                print(
                    f"  {code} queries: {self._n_success} succeeded, "
                    f"{self._n_failed} failed. Logs: {self.run_log_dir}",
                    flush=True,
                )
            finally:
                browser.close()

    # ---- search helpers -----------------------------------------------

    def _search_zips(self, page, batch: list[str]) -> tuple[list[dict], int] | None:
        """Search a zip batch. On `statusCode 117` (invalid zip in batch),
        retry one zip at a time so the valid ones don't get dropped.

        Returns `(offenders, invalid_count)` or `None` on hard failure.
        """
        resp = self._post_search(page, {"zips": batch, "clientIp": ""})
        if resp is None:
            return None
        if resp.get("statusCode") == 117:
            return self._search_singletons(page, batch)
        if resp.get("statusCode") == 511:
            # The zip slice exceeded the result cap; chop. Each single-zip
            # call should fit. If a single zip still trips 511 we drop it.
            return self._search_singletons(page, batch)
        return resp.get("offenders") or [], 0

    def _search_singletons(self, page, batch: list[str]) -> tuple[list[dict], int]:
        merged: list[dict] = []
        invalid = 0
        for z in batch:
            resp = self._post_search(page, {"zips": [z], "clientIp": ""})
            if resp is None:
                invalid += 1
                continue
            sc = resp.get("statusCode")
            if sc in (117, 511):
                invalid += 1
            elif sc in (200, 201):
                merged.extend(resp.get("offenders") or [])
            time.sleep(self.request_delay_s)
        return merged, invalid

    def _search_name_prefix(self, page, first: str, last: str) -> list[dict]:
        """Name-prefix search scoped to this jurisdiction. On a 511
        ("too many results") the surname prefix is extended by one
        letter and recursed. Single-extension recursion is enough in
        practice; if a 2-char prefix still trips 511 the records are
        dropped."""
        code = self.jurisdiction_code
        body = {
            "firstName": first,
            "lastName": last,
            "jurisdictions": [code],
            "clientIp": "",
        }
        resp = self._post_search(page, body)
        if resp is None:
            return []
        if _jur_capped(resp, code):
            merged: list[dict] = []
            for extra in NAME_LETTERS:
                sub = self._post_search(
                    page,
                    {
                        "firstName": first,
                        "lastName": last + extra,
                        "jurisdictions": [code],
                        "clientIp": "",
                    },
                )
                if sub is not None and not _jur_capped(sub, code):
                    merged.extend(sub.get("offenders") or [])
                time.sleep(self.request_delay_s)
            return merged
        return resp.get("offenders") or []

    def _post_search(self, page, body: dict) -> dict | None:
        """Run a single POST from inside the page's JS context. Logs every
        attempt to either successful.jsonl or failed.jsonl. Returns the
        parsed JSON body (whether success or known API error like 117) so
        the caller can decide on retry strategy, or `None` on hard
        transport failure.

        Important: no Content-Type header. The original nsopw.gov client
        leaves it as the jQuery default so the browser sends a "simple"
        POST without CORS preflight — fewer round trips means fewer
        chances for CF to drop CORS headers on the response. We match
        that pattern."""
        try:
            raw = page.evaluate(
                """async (args) => {
                    try {
                        const r = await fetch(args.url, {
                            method: 'POST',
                            body: JSON.stringify(args.body),
                        });
                        const text = await r.text();
                        return {ok: true, status: r.status, body: text};
                    } catch (e) {
                        return {ok: false, error: e.toString()};
                    }
                }""",
                {"url": API_URL, "body": body},
            )
        except Exception as e:
            self._log_failed(body, "playwright_exception", str(e), None, retryable=True)
            self._on_failure()
            return None
        if not raw:
            self._log_failed(body, "no_eval_result", None, None, retryable=True)
            self._on_failure()
            return None
        if not raw.get("ok"):
            err = raw.get("error") or ""
            # `Failed to fetch` typically means CORS-stripped response —
            # CF is rate-limiting. Retry-worthy.
            self._log_failed(body, "fetch_threw", err, None, retryable=True)
            self._on_failure()
            return None
        http_status = raw.get("status")
        try:
            parsed = json.loads(raw.get("body") or "")
        except json.JSONDecodeError as e:
            self._log_failed(body, "json_decode", str(e), http_status, retryable=True)
            return None
        api_sc = parsed.get("statusCode")
        if api_sc in (200, 201):
            code = self.jurisdiction_code
            jur_records = sum(
                j.get("records", 0)
                for j in (parsed.get("jurisdictionStatus") or [])
                if j.get("jurisdictionId") == code
            )
            self._log_success(body, http_status, api_sc, jur_records, _jur_capped(parsed, code))
            self._consecutive_failures = 0
            return parsed
        if api_sc == 117:
            self._log_failed(body, "api_117_invalid_zip", None, http_status, retryable=False)
            return parsed
        if api_sc == 511:
            self._log_failed(body, "api_511_over_cap", None, http_status, retryable=True)
            return parsed
        self._log_failed(
            body, f"api_sc_{api_sc}", parsed.get("message"), http_status, retryable=True
        )
        return parsed

    def _log_success(
        self,
        body: dict,
        http_status: int | None,
        api_sc: int | None,
        jur_records: int,
        jur_capped: bool,
    ) -> None:
        if self._success_f is None:
            return
        self._success_f.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "query": body,
                    "http_status": http_status,
                    "api_status": api_sc,
                    "jur_records": jur_records,
                    "jur_capped": jur_capped,
                }
            )
            + "\n"
        )
        self._n_success += 1

    def _log_failed(
        self,
        body: dict,
        reason: str,
        detail: str | None,
        http_status: int | None,
        retryable: bool,
    ) -> None:
        if self._failed_f is None:
            return
        self._failed_f.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "query": body,
                    "reason": reason,
                    "detail": detail,
                    "http_status": http_status,
                    "retryable": retryable,
                }
            )
            + "\n"
        )
        self._n_failed += 1

    def _on_failure(self) -> None:
        """Pace down after a rate-limited failure: sleep an extra
        `failure_backoff_s` so the next call has room to land. After N
        consecutive failures, re-navigate the homepage to refresh
        Cloudflare cookies."""
        self._consecutive_failures += 1
        time.sleep(self.failure_backoff_s)
        if (
            self._page is not None
            and self._consecutive_failures >= self.renav_after_consecutive_failures
        ):
            try:
                code = self.jurisdiction_code
                print(
                    f"  {code}: {self._consecutive_failures} consecutive failures — "
                    f"re-navigating to refresh CF session",
                    flush=True,
                )
                self._page.goto(HOMEPAGE, wait_until="networkidle", timeout=60_000)
                self._page.wait_for_timeout(3000)
                self._consecutive_failures = 0
            except Exception as e:
                code = self.jurisdiction_code
                print(f"  {code}: re-navigation failed: {e}", flush=True)

    # ---- normalize ----------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None

        name = raw.get("name") or {}
        full_name = " ".join(
            part
            for part in (
                name.get("givenName"),
                name.get("middleName"),
                name.get("surName"),
            )
            if part
        ).strip() or "UNKNOWN"

        aliases = []
        for a in raw.get("aliases") or []:
            alias = " ".join(
                p for p in (a.get("givenName"), a.get("middleName"), a.get("surName")) if p
            ).strip()
            if alias:
                aliases.append(alias)

        source_id = raw.get("offenderUri") or _stable_id(raw)
        default_state = self.jurisdiction_code

        addresses: list[Address] = []
        for loc in raw.get("locations") or []:
            lat = loc.get("latitude") or None
            lon = loc.get("longitude") or None
            # NSOPW emits 0/0 for "unknown" — drop those, don't pretend it's Africa.
            if lat == 0 and lon == 0:
                lat, lon = None, None
            addresses.append(
                Address(
                    type="home" if loc.get("type") == "R" else "other",
                    street=loc.get("streetAddress") or None,
                    city=loc.get("city") or None,
                    state=loc.get("state") or default_state,
                    zip=loc.get("zipCode") or None,
                    lat=lat,
                    lon=lon,
                )
            )

        registration = Registration(
            status="absconder" if raw.get("absconder") else "active",
            absconder=bool(raw.get("absconder")),
        )

        return OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=str(source_id),
                source_url=API_URL,
                info_url=raw.get("offenderUri"),
                fetched_at=self._fetched_at,
            ),
            identity=Identity(
                full_name=full_name,
                aliases=aliases,
                sex=_normalize_sex(raw.get("gender")),
                # NSOPW returns current `age` (no DOB). Derive an approximate
                # birth year from the fetch date so downstream gets an age axis.
                # race/height/weight/eyes/hair are NOT in the NSOPW API — they
                # live on the per-offender state detail page and are filled by
                # the detail-enrichment pass.
                year_of_birth=_yob_from_age(raw.get("age"), self._fetched_at),
            ),
            addresses=addresses,
            registration=registration,
            raw=raw,
        )

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        url = raw.get("imageUri")
        if not url:
            return []
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


# ---------------------------------------------------------------------------
# Module-level helpers


def _chunked(items: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _jur_capped(resp: dict, code: str) -> bool:
    """True if either the top-level response or this jurisdiction's
    sub-response returned 511 (too many results)."""
    if resp.get("statusCode") == 511:
        return True
    for j in resp.get("jurisdictionStatus") or []:
        if j.get("jurisdictionId") == code and str(j.get("statusCode")) == "511":
            return True
    return False


def _yob_from_age(age: object, fetched_at: datetime | None) -> int | None:
    """Approximate birth year from NSOPW's current `age` and the fetch date.
    Returns None for missing / out-of-range ages."""
    try:
        a = int(age)
    except (TypeError, ValueError):
        return None
    if a <= 0 or a > 120:
        return None
    year = (fetched_at or datetime.now(timezone.utc)).year
    return year - a


def _normalize_sex(value: object) -> str:
    if not value:
        return "unknown"
    v = str(value).strip().upper()
    if v in {"M", "MALE"}:
        return "M"
    if v in {"F", "FEMALE"}:
        return "F"
    if v in {"X", "U"}:
        return "X"
    return "unknown"


def _stable_id(raw: dict) -> str:
    """Last-resort ID when offenderUri is missing — name + first zip."""
    name = raw.get("name") or {}
    parts = [
        name.get("surName") or "?",
        name.get("givenName") or "?",
        str((raw.get("locations") or [{}])[0].get("zipCode") or "?"),
    ]
    return ":".join(parts)
