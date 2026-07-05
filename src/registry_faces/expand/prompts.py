"""Prompt builder for the sex-offender-registry per-country discovery loop.

Reads the generic ``web_scrubber.expand.Target`` — ``.fields`` holds the raw
ledger row (Country/CountryName/...), ``.key[0]`` is the ISO code.
"""
from __future__ import annotations

from web_scrubber.expand import Target


def build_prompt(t: Target, adapter_name: str, registered: list[str],
                 theory: str | None = None) -> str:
    cc = (t.fields.get("Country") or (t.key[0] if t.key else "")).upper()
    name = t.fields.get("CountryName") or t.label
    have = ", ".join(sorted(registered)) or "(none)"
    hint = ""
    if theory:
        hint = ("\nRESEARCH HINTS — this country was marked unsupported before; a "
                "failure-analysis pass suggested these approaches. Try them before "
                f"giving up:\n  {theory}\n")
    return f"""\
You are extending the registry-faces sex-offender-registry collection. Add coverage
for ONE country and then STOP. Do NOT run git — the orchestrator verifies + records.

TARGET COUNTRY : {name} ({cc})
NEW ADAPTER NAME : {adapter_name}  (write adapters_generated/{adapter_name}.py)
ALREADY-BUILT ADAPTERS (do not duplicate) : {have}
{hint}
IMPORTANT CONTEXT: a PUBLIC, individually-searchable sex-offender registry is rare
outside the United States — most countries keep such records police-only by law.
So "unsupported" is the expected, correct outcome for most countries. Only mark a
country covered if an OFFICIAL government registry publishes offender records to the
general public in an enumerable form.

STEP 1 — Read the conventions:
  - src/registry_faces/adapters/base.py    (Adapter: fetch/normalize/extract_photos + build())
  - src/registry_faces/adapters/hawaii.py  (reference adapter — mirror its structure)
  - src/registry_faces/schema.py           (the OffenderRecord schema)

STEP 2 — Determine whether {name} operates a PUBLIC sex-offender registry that:
  - is run by an official government authority (not an aggregator), AND
  - lets the general public browse/enumerate offender records (a list-all or a
    paginable search returning records), serving name + ideally photo/offense.
  Use web search + fetch. NOT acceptable: police-only/login-gated systems, name-only
  one-off lookups with no enumeration, CAPTCHA walls, or third-party sites.

STEP 3 — Produce exactly ONE outcome:
  (A) BUILT — write adapters_generated/{adapter_name}.py: an Adapter subclass with
      jurisdiction = "{cc}" (or "{cc}-<REGION>" if sub-national),
      fetch()/normalize()/extract_photos() mapping the registry to OffenderRecord,
      and a module-level build(). extract_photos returns only source-served image URLs.
  (B) UNSUPPORTED — if {name} has no qualifying public registry (the common case),
      make NO code changes and state why (police-only / no public registry / law
      prohibits / search-only / captcha / etc.).

STEP 4 — Finish with a single fenced ```json block and nothing after it:
```json
{{"outcome": "built|unsupported",
  "name": "{adapter_name}",
  "jurisdiction": "{cc} or '{cc}-<REGION>' or ''",
  "source_url": "<the registry URL you used, or ''>",
  "confidence": <0.0-1.0: for 'unsupported', how sure you are NO public registry exists>,
  "reason": "<for unsupported: short reason>"}}
```
"""


__all__ = ["build_prompt"]
