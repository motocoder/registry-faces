"""Tennessee TBI sex offender registry adapter.

Source: https://sor.tbi.tn.gov

Plain JSON API behind the React SPA — no captcha, no login, no
disclaimer cookie. Pagination via `/api/search/{start}/{count}`; the
server caps `count` at 100 (anything larger silently falls back to 10).
A single empty-query call to `/api/search/0/1` returns the total
registrant count in the `hits` field, so we don't need to enumerate
queries — just walk pages 0..hits in steps of 100.

One quirk to handle: the TN server is misconfigured and serves only its
leaf cert, omitting the Entrust intermediate. The AIA extension on the
leaf points at the missing intermediate's download URL, so we fetch it
once, cache it next to the registry, and append it to certifi's bundle.
After that all calls verify normally.

Schema mapping highlights:
  * `soid` -> `source.source_id`
  * `offender.secta.{lastname, firstname, aliasnames, dob, sex, race,
    height, weight, eye, hair, smts}` -> Identity
    (height is FDLE-style `feet*100 + inches`, same as Florida)
  * `offender.sectb.primary` -> Address(type="home")
    `offender.sectb.secondary` -> Address(type="temporary")
    `offender.secte.employers[]` -> Address(type="work")
  * `offender.sectf.offenses[]` -> Offense
  * `offender.secth.statuscode` -> Registration.status
  * `offender.sorimage.url` -> PhotoRef (relative path; we prefix
    PUBSOR_BASE)
"""

from __future__ import annotations

import base64
import re
import ssl
import time
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import certifi
import httpx

from ..photos import PhotoRef
from ..schema import Address, Identity, Offense, OffenderRecord, Registration, Source
from .base import Adapter

BASE_URL = "https://sor.tbi.tn.gov"
SEARCH_URL_TEMPLATE = f"{BASE_URL}/api/search/{{start}}/{{count}}"
PAGE_SIZE = 100  # server-side cap; larger silently falls back to 10

# Where we cache the fetched intermediate cert and combined CA bundle.
CA_BUNDLE_DIR = Path("registry-runs/tennessee")
CA_BUNDLE_PATH = CA_BUNDLE_DIR / "ca-bundle.pem"

# secth.statuscode -> canonical Registration.status. Distribution
# observed in a 5,000-record sample (codes that didn't appear are not
# mapped; if they show up later they fall through to "unknown" and the
# raw `status` string survives in `record.raw`).
_STATUS_MAP: dict[str, str] = {
    "A":  "active",
    "AI": "active",     # ACTIVE - INCAPACITATED
    "AR": "active",     # ACTIVE - RESIDES OUT OF STATE, EMPLOYED IN STATE
    "B":  "absconder",
    "ID": "deceased",   # INACTIVE - DECEASED
    "II": "incarcerated",  # INACTIVE - INCARCERATED
    "IM": "removed",    # INACTIVE - MOVED TO ANOTHER STATE
    "IP": "removed",    # INACTIVE - DEPORTED
}

# secta.sex string -> canonical Sex literal
_SEX_MAP = {
    "MALE": "M",
    "M": "M",
    "FEMALE": "F",
    "F": "F",
}


class TennesseeAdapter(Adapter):
    jurisdiction = "US-TN"
    source_name = "Tennessee Bureau of Investigation"

    def __init__(
        self,
        page_size: int = PAGE_SIZE,
        request_timeout: float = 90.0,
        retry_attempts: int = 3,
        retry_backoff: float = 2.0,
    ) -> None:
        self.page_size = page_size
        self.request_timeout = request_timeout
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self._fetched_at: datetime | None = None
        self._client: httpx.Client | None = None
        self._total_hits: int | None = None

    # ---- fetch ---------------------------------------------------------

    def fetch(self) -> Iterator[dict]:
        self._fetched_at = datetime.now(timezone.utc)
        verify = _resolve_ca_bundle()
        self._client = httpx.Client(
            timeout=self.request_timeout,
            verify=verify,
            headers={
                "User-Agent": "Mozilla/5.0 registry-faces/0.1",
                "Accept": "application/json",
            },
        )
        try:
            # Probe to learn the total hit count.
            first = self._get_page(0, 1)
            self._total_hits = int(first["hits"])
            for start in range(0, self._total_hits, self.page_size):
                page = self._get_page(start, self.page_size)
                inner = page["searchresults"]
                rows = inner if isinstance(inner, list) else _loads_searchresults(inner)
                for row in rows:
                    yield row
        finally:
            self._client.close()
            self._client = None

    def _get_page(self, start: int, count: int) -> dict:
        assert self._client is not None
        url = SEARCH_URL_TEMPLATE.format(start=start, count=count)
        last_err: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                r = self._client.get(url)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                time.sleep(self.retry_backoff * (attempt + 1))
        raise RuntimeError(f"TN API failure after {self.retry_attempts} attempts: {last_err}") from last_err

    # ---- normalize -----------------------------------------------------

    def normalize(self, raw: dict) -> OffenderRecord:
        assert self._fetched_at is not None
        offender = raw.get("offender") or {}
        soid = str(raw.get("soid") or offender.get("soid") or "").strip()
        secta = offender.get("secta") or {}
        sectb = offender.get("sectb") or {}
        sectf = offender.get("sectf") or {}
        secte = offender.get("secte") or {}
        secth = offender.get("secth") or {}

        first = (secta.get("firstname") or "").strip()
        last = (secta.get("lastname") or "").strip()
        full_name = " ".join(p for p in (first, last) if p) or (
            secta.get("offname") or "UNKNOWN"
        )

        aliases = [a for a in (secta.get("aliasnames") or []) if isinstance(a, str) and a.strip()]
        marks = secta.get("smts") or []
        description = "; ".join(m for m in marks if isinstance(m, str) and m) or None

        dob = _parse_date(secta.get("dob"))

        identity = Identity(
            full_name=full_name,
            aliases=aliases,
            dob=dob,
            year_of_birth=dob.year if dob else None,
            sex=_SEX_MAP.get((secta.get("sex") or "").strip().upper(), "unknown"),
            race=(secta.get("race") or "").strip() or None,
            height_cm=_height_cm(secta.get("height")),
            weight_kg=_weight_kg(secta.get("weight")),
            eye_color=(secta.get("eye") or "").strip() or None,
            hair_color=(secta.get("hair") or "").strip() or None,
            description=description,
        )

        addresses: list[Address] = []
        primary = sectb.get("primary") or {}
        a = _residence_address(primary, "home")
        if a is not None:
            addresses.append(a)
        secondary = sectb.get("secondary") or {}
        a = _residence_address(secondary, "temporary")
        if a is not None:
            addresses.append(a)
        for emp in secte.get("employers") or []:
            a = _employer_address(emp)
            if a is not None:
                addresses.append(a)

        offenses: list[Offense] = []
        for off in sectf.get("offenses") or []:
            desc = (off.get("description") or off.get("offense") or "").strip()
            if not desc:
                continue
            offenses.append(
                Offense(
                    raw_description=desc,
                    conviction_date=_parse_iso(off.get("offensedate"))
                    or _parse_date(off.get("offensedatestr")),
                    statute=(off.get("tcacode") or "").strip() or None,
                )
            )

        status_code = (secth.get("statuscode") or "").strip().upper()
        status = _STATUS_MAP.get(status_code, "unknown")
        classification = (secth.get("classification") or "").strip() or None

        registration = Registration(
            status=status,  # type: ignore[arg-type]
            absconder=(status == "absconder"),
        )

        record = OffenderRecord(
            source=Source(
                jurisdiction=self.jurisdiction,
                source_id=soid,
                source_url=BASE_URL,
                info_url=f"{BASE_URL}/offender/{soid}",
                fetched_at=self._fetched_at,
            ),
            identity=identity,
            addresses=addresses,
            offenses=offenses,
            registration=registration,
            raw=raw,
        )
        # Stash classification separately — schema has no tier field on
        # registration, so keep the raw value in `raw` (already there)
        # and surface a top-level hint via offenses' tier where it
        # naturally belongs (skipped — TN classification is per person,
        # not per offense).
        _ = classification
        return record

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        sorimage = (raw.get("offender") or {}).get("sorimage") or {}
        url = (sorimage.get("url") or "").strip()
        if not url:
            return []
        if not url.startswith("http"):
            url = BASE_URL + (url if url.startswith("/") else "/" + url)
        return [
            PhotoRef(
                url=url,
                source_type="registry",
                source_name=self.source_name,
            )
        ]


# ---------------------------------------------------------------------------
# CA bundle handling


def _resolve_ca_bundle() -> str:
    """Return a path to a CA bundle that can verify sor.tbi.tn.gov.

    The TN server omits the Entrust DV TLS Issuing RSA CA 2 intermediate
    from its TLS handshake. We fetch that intermediate via the AIA
    extension on the leaf cert once, append it to certifi's bundle, and
    cache the combined file. Subsequent runs reuse the cache.
    """
    if CA_BUNDLE_PATH.exists() and CA_BUNDLE_PATH.stat().st_size > 0:
        return str(CA_BUNDLE_PATH)

    CA_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

    # Pull the leaf cert so we can read its AIA URL.
    leaf_pem = ssl.get_server_certificate(("sor.tbi.tn.gov", 443))
    aia_url = _extract_ca_issuers_url(leaf_pem)
    if not aia_url:
        raise RuntimeError(
            "Could not find a CA Issuers AIA URL on the TN leaf certificate; "
            "cannot bootstrap a verifying CA bundle."
        )

    with urllib.request.urlopen(aia_url, timeout=60) as r:
        intermediate_der = r.read()
    intermediate_pem = _der_to_pem(intermediate_der)

    combined = Path(certifi.where()).read_text() + intermediate_pem
    CA_BUNDLE_PATH.write_text(combined)
    return str(CA_BUNDLE_PATH)


def _extract_ca_issuers_url(leaf_pem: str) -> str | None:
    # ssl module doesn't parse extensions, so parse via the openssl text
    # output reachable through SSLContext is overkill — do a tiny parse
    # of the AIA URL from the DER ASN.1 by going through `ssl` is
    # painful. Easier: shell out to openssl if available; otherwise
    # fall back to a known-good URL.
    import shutil
    import subprocess
    if shutil.which("openssl"):
        try:
            text = subprocess.check_output(
                ["openssl", "x509", "-text", "-noout"],
                input=leaf_pem.encode(),
                stderr=subprocess.DEVNULL,
            ).decode()
            m = re.search(r"CA Issuers - URI:(\S+)", text)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    # Last-resort known URL for the Entrust DV TLS Issuing RSA CA 2.
    return "http://crt.sectigo.com/EntrustDVTLSIssuingRSACA2.crt"


def _der_to_pem(der: bytes) -> str:
    b64 = base64.b64encode(der).decode()
    lines = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


# ---------------------------------------------------------------------------
# Field helpers


def _loads_searchresults(value):
    import json
    if not value:
        return []
    return json.loads(value)


def _parse_date(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    for sep in ("/", "-"):
        m = re.fullmatch(rf"(\d{{1,2}}){re.escape(sep)}(\d{{1,2}}){re.escape(sep)}(\d{{4}})", s)
        if m:
            month, day, year = (int(g) for g in m.groups())
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _height_cm(value: object) -> float | None:
    """TN encodes height as feet*100 + inches, same as Florida (e.g. 509 = 5'9")."""
    if not value:
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    n = int(s)
    feet, inches = divmod(n, 100)
    if not (3 <= feet <= 8) or not (0 <= inches <= 11):
        return None
    return round((feet * 12 + inches) * 2.54, 1)


def _weight_kg(value: object) -> float | None:
    if not value:
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    pounds = int(s)
    if pounds <= 0 or pounds > 1000:
        return None
    return round(pounds * 0.45359237, 1)


def _residence_address(d: dict, addr_type: str) -> Address | None:
    street = (d.get("street") or "").strip()
    city = (d.get("city") or "").strip()
    state = (d.get("state") or "").strip()
    zipcode = (d.get("zipcode") or d.get("zip") or "").strip()
    county = (d.get("county") or "").strip()
    if not any((street, city, state, zipcode)):
        return None
    addr = Address(
        type=addr_type,  # type: ignore[arg-type]
        street=street or None,
        city=city or None,
        state=state or None,
        zip=zipcode or None,
        verified_at=_parse_date(d.get("startdate")),
    )
    # Preserve county via the raw dict — Address has no county field.
    _ = county
    return addr


def _employer_address(emp: dict) -> Address | None:
    street = (emp.get("street") or "").strip()
    city = (emp.get("city") or "").strip()
    state = (emp.get("state") or "").strip()
    zipcode = (emp.get("zip") or emp.get("zipcode") or "").strip()
    if not any((street, city, state, zipcode)):
        return None
    return Address(
        type="work",
        street=street or None,
        city=city or None,
        state=state or None,
        zip=zipcode or None,
    )


def build() -> TennesseeAdapter:
    return TennesseeAdapter()
