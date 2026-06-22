# HANDOFF — backfill registry physical specs (race/eyes/hair/height/weight/age)

**Run this on the Windows dev box (`C:/development`)** — it needs Playwright +
chromium + the HBase tunnel, which the Mac checkout doesn't have. Goal: fill the
missing physical specs for **all registry (NSOPW) states** into HBase.

## Why this is needed
The NSOPW search API the 35 NSOPW states use returns only
`name/gender/age/location/image/offenderUri` — **race, eyes, hair, height,
weight are NOT in it.** They live on each offender's *state detail page*
(`offenderUri`). So they have to be fetched per-offender and parsed. (~9 direct
states like NC/GA already scrape these; the ~35 NSOPW states don't.)

Today ~72% of registry persons have null race; same story for eyes/hair/height/
weight. `age` is the one field the API does carry.

## What's already built + tested (code-complete, committed pending your OK)
- **`_nsopw.py`** now derives `year_of_birth` from the API `age` → all 35 NSOPW
  states get **age** on their next scrape (no detail fetch needed).
- **`web_scrubber/physical.py`** — value normalizers (`5'05''`→cm, `164lbs`→kg,
  `Am Ind`→race, eye/hair codes). Tested.
- **`web_scrubber/detail_extract.py`** — generic deterministic extractor that
  reads race/sex/height/weight/eyes/hair/age from inline-bullet, table, and
  definition-list layouts (covers all states; no LLM). Tested.
- **`web_scrubber/person/detail_enrich.py`** — the pass: for each person missing
  fields, fetch detail page → extract → fill ONLY gaps → `put_person`. Idempotent;
  supports `kinds` (registry-only) and `shard` (parallel). Tested.
- **`registry-faces enrich-details`** CLI — wires the HBase store + a
  Cloudflare-aware `BrowserFetcher` + the pass.

> Tests run with `PYTHONPATH=src:../web-scrubber/src` via a venv that has both
> installed (155 web-scrubber + 38 registry-faces passing on the Mac).

## Prereqs on the Windows box
```powershell
cd C:/development/registry-faces
.venv\Scripts\pip install -e ..\web-scrubber          # web_scrubber must be importable
.venv\Scripts\pip install -e ".[all]"
.venv\Scripts\playwright install chromium
# bring up the HBase tunnel the nightly uses; confirm 127.0.0.1:9090 is open:
C:/development/web-scrubber/shared-root/hbase/hbase-tunnel.ps1
```
`identity.properties` is already set to `identity.mode=hbase`, `hbase.host=localhost`,
`hbase.port=9090`.

## Step 1 — spot-check FIRST (validate the extractor on real pages)
```powershell
registry-faces enrich-details --to hbase --kind registry --limit 5 --pause 1.0
```
Watch `scanned/enriched`, then confirm a few persons actually got race/eyes/hair/
height/weight (e.g. `GET https://www.registryrecognizer.com/pins/person/<uuid>`
after a sync, or read the HBase `person` `i:` cell). The extractor is unit-tested
on representative HTML but **not yet on live state pages** — if a state parses
poorly, note it (it can get a per-jurisdiction override parser later).

## Step 2 — full backfill, parallel
~600k registry persons need a detail fetch each; serial at a polite rate is
**days/weeks**. Use `--shard i/N` to run N processes concurrently (stable hash
split, disjoint slices). Example, 20 ways:
```powershell
# launch 20 shards (each its own window/job); ~20x throughput
for ($i=0; $i -lt 20; $i++) {
  Start-Process -NoNewWindow registry-faces `
    "enrich-details --to hbase --kind registry --shard $i/20 --pause 0.5"
}
```
- **Idempotent / resumable** — re-running skips persons that already have the
  data, so you can stop/restart or re-run shards that died.
- `--kind registry` means it ignores wanted/missing/booking persons.
- Tune `--pause` up if a state's site rate-limits/Cloudflare-bounces.

## Step 3 — make it stick going forward
After each **registry sweep** (`ingest-identity`), chain the enrich pass so new
offenders get specs — do this in the registry-scrub run script, **NOT** the
nightly (the nightly is bookings + news only):
```powershell
registry-faces ingest-identity <state> --to hbase
registry-faces enrich-details   --to hbase --kind registry
```
(Age already lands automatically via the `_nsopw.py` fix on the next scrape.)

## Step 4 — surface it
Once a meaningful chunk is filled, re-sync registry-server so the map/detail/stats
reflect it:
```
POST https://www.registryrecognizer.com/admin/pins/sync?full=true
```
Then the Statistics race chart should shift off "Unknown" and pin detail shows
the new fields.

## Caveats
- Generic extractor = deterministic, covers the common label/table/dl layouts.
  Per-state override parsers can be added where a page defeats it (extension
  point in `web_scrubber.person.detail_enrich` / `detail_extract`).
- The same `enrich-details` exists on missing-faces/wanted-faces (defaults
  `--kind missing` / `wanted`) but you said registry-only for now — those already
  get most specs from their source APIs anyway.

---

# SEPARATE FIX — map cluster piling (`regeocode-hbase`)

Symptom: a map cluster of tens of thousands that won't break when you zoom (just
zooms to max and never fans). Cause: registry records that only resolved to a
**state** were placed on the bare **state centroid**, so they share one identical
coordinate (e.g. ~66k on the FL centroid, all `geo_precision=state`). The map
can't separate identical points by zoom, and can't fan that many.

The geocoder now disperses `state`-tier points across the state's bounding box;
this data just predates it. Re-geocode it once:

```powershell
cd C:/development/registry-faces && git pull   # gets the new command
registry-faces regeocode-hbase --dry-run       # preview the precision breakdown
registry-faces regeocode-hbase                 # disperse registry addrs, writes HBase
```
- Scans the `person` table, re-runs the dispersing geocoder over every **registry**
  attachment (`a:criminal:registry:*`), keeps real `exact` coords, bumps `i:json`
  so the map delta-sync notices. Takes the global ingest lock; re-run if held.
- After it finishes, surface it: `POST https://www.registryrecognizer.com/admin/pins/sync?full=true`.

Result: each state's records scatter across the state → clusters break apart as
you zoom → genuinely same-address handfuls fall under the spiderfy cap and fan
out normally.
