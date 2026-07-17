# registry-faces — Agent Instructions

## Overview

A personal index over public US sex offender registry data, normalized
across jurisdictions onto one Pydantic schema. As of the web-scrubber
merge, this repo is a **domain consumer** of a shared scraping/storage
framework — most of the engine now lives in a sibling repo.

**Read [`LEGAL.md`](LEGAL.md) before generating or running any adapter** —
it covers scope constraints (personal lookup only, no republishing, no
facial-recognition aggregation) and per-state ToS considerations. The
desktop app gates first launch on acknowledging it.

## Commands

```powershell
# Setup (Python 3.11+). The web-scrubber sibling MUST be installed first.
python -m venv .venv
.venv\Scripts\pip install -e ..\web-scrubber       # the engine — required
.venv\Scripts\pip install -e ".[all]"              # providers + UI + tests + shards

# Tests
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\pytest.exe tests\test_store_merge.py::test_name -q   # one test

# Run the desktop UI (registry rooted at .\registry)
registry-faces --registry .\registry ui

# Build an adapter via the agent (writes adapters_generated/<name>.py)
registry-faces build URL --name texas --jurisdiction US-TX [--provider gemini]

# --- Path A: local store -> shards -> R2 -> Android client ---
registry-faces ingest texas             # adapter -> local records/, idempotent merge
registry-faces sync-photos [--refresh]  # download pending photos into photos/
registry-faces verify / rebuild-index / lookup "Smith" / near LAT LON / stats
.venv\Scripts\python.exe scripts\package_shards.py            # records/ -> shards/US-XX/*.zip
.venv\Scripts\python.exe scripts\upload_shards.py [--dry-run] # shards/ -> Cloudflare R2

# --- Path B: adapter -> centralized person identity (file dry-run or HBase) ---
registry-faces ingest-identity texas --to file        # -> ./identity-store (dry-run)
registry-faces ingest-identity texas --to hbase       # -> HBase (prod)
registry-faces sync-identity-photos --to file         # photo bytes -> BlobStore
```

```sh
# POSIX equivalent
python -m venv .venv
.venv/bin/pip install -e ../web-scrubber
.venv/bin/pip install -e '.[all]'
.venv/bin/pytest -q
./run.sh   # desktop UI helper script; registry rooted at ./registry
```

Every Path-A command takes a global `-r/--registry PATH` (default
`./registry`). Path B is configured by `identity.properties` (mode
file|hbase, store roots, HBase host/port); `--to` overrides the mode. There
is no configured linter. CI's verify command for this project is
`python -m pytest -q`.

## Architecture

### Two-repo architecture (read this first)

The core was extracted into **`web-scrubber`**, a sibling repo installed
editable (`pip install -e ../web-scrubber`, declared as a dependency in
`pyproject.toml`). A fresh clone does NOT build without it checked out next
to this one.

- **`web-scrubber`** = the engine. Generic, not registry-specific. Holds the
  `FileStore`/index/manifest/merge engine, the photo sync engine, the agent
  provider plumbing + tools, fetch helpers (http/browser/pdf), and the
  centralized **person identity** subsystem (`web_scrubber.person`: file +
  HBase backends, ingest, merge, photosync, entity `resolve`, models,
  config). It also hosts an unrelated `article` domain — it's a general
  platform.
- **`registry-faces`** (this repo) = the sex-offender domain. It owns
  `schema.py` (`OffenderRecord`), the state `adapters/`, the `desktop/` UI,
  the shard `scripts/`, and `identity_map.py`. Everything else is a **thin
  binding**.

### The binding layer — what's local vs re-exported

These modules look like the originals but now mostly re-export
`web_scrubber`, keeping only the registry-specific parts. Don't add engine
logic here; it belongs in `web-scrubber`.

- `store.py` — re-exports the framework `FileStore`; keeps the **domain
  merge rules** (how two `OffenderRecord`s merge: address/offense keys,
  null-never-overwrites) via a `StoreSpec`, plus geo search
  (`search_radius`) and the `backfill_guids` migration. On-disk layout
  unchanged: `records/<jur>/<id>/`.
- `photos.py` — re-exports the manifest/sync/verify engine; keeps only the
  `PhotoRef` default (`source_type="registry"` -> `NNN-registry.jpg`) and
  the historical `sync_photos` signature.
- `agent/tools.py`, `agent/builder.py`, `agent/providers/`,
  `agent/tool_schema.py` — bindings that point the framework agent at this
  repo's `AgentContext` (schema path, Hawaii reference adapter, output
  dir).

### Two output paths — know which one you're touching

There are now **two parallel destinations** for the same adapter output,
and it is not yet settled whether B supersedes A or they coexist:

| | Path A (original) | Path B (newer) |
|---|---|---|
| CLI | `ingest`, `sync-photos` | `ingest-identity`, `sync-identity-photos` |
| Store | local `records/` JSON, **source-keyed** | central person store, **person-keyed**, with entity resolution |
| Backend | filesystem | file dry-run *or* **HBase** |
| Merge | `web_scrubber.merge` (per source record) | `web_scrubber.person.merge` (resolved persons) |
| Downstream | `package_shards` -> R2 -> Android app | HBase identity graph |

`identity_map.py` is the bridge into Path B: it maps one `adapter.run()`
item (`OffenderRecord`, `list[PhotoRef]`) -> `(Person, RegistryAttachment,
photos)`, e.g. scalar height/weight become `min==max` ranges on the
canonical `Person`.

### Adapters

One module per source under `adapters/`, each subclassing `Adapter`
(`adapters/base.py`, still local) with the three-phase pipeline: `fetch()`
-> raw dicts, `normalize(raw)` -> `OffenderRecord`, `extract_photos(raw)`
-> `PhotoRef`s (source-published URLs only). Each exposes module-level
`build()`.

- Most states are thin **`NsopwAdapter`** subclasses (`adapters/_nsopw.py`)
  — set `jurisdiction_code` + `zip_range` and inherit the Playwright-driven
  NSOPW fetch (drives headless Chromium past Cloudflare, then ZIP sweep +
  26x26 name sweep). A handful (FL, SD, etc.) are bespoke. `hawaii.py` is
  the reference.
- Coverage: 42 states present. **Missing: TX, VT, VA, WV, WI.** CA/NY/MA
  are intentionally excluded (see the blacklist under Project rules).
- `cli._load_adapter` resolves a name by trying the in-package `adapters/`
  first, then generated `adapters_generated/<name>.py` (gitignored).

For depth on install / providers / credentials, see
[`docs/usage.md`](docs/usage.md); field-level and on-disk references are in
[`docs/schema.md`](docs/schema.md) and
[`docs/storage-layout.md`](docs/storage-layout.md).

## Project rules

- Photos: only download URLs the source registry's own payload published.
  No image search/scrape. `extract_photos` must respect this.
- No URL fetching at normalize time; `lat`/`lon` left empty unless the
  source gives them.
- No cross-jurisdiction normalization of offense tier — store
  `tier_or_level_raw` verbatim.
- **Blacklist (NY/CA/MA):** currently enforced ONLY in
  `scripts/package_shards.py` (`SHARD_BLACKLIST`), i.e. Path A shard
  builds. The Path B `ingest-identity` flow does NOT consult it — if the
  blacklist is a hard policy constraint, that gap needs closing before
  pushing identity data for those states.
- `identity.properties` at the repo root is working config, not a finished
  artifact.
- This project consumes **web-scrubber** as a source sibling: install it
  editable (`pip install -e ../web-scrubber`) before this package. CI does
  the same automatically via the catalog's `python_source_dependencies`
  (`server-config/ci/catalog/projects.json`).
- Git workflow (branching, commits, pushes, versioning) is governed by the
  workspace session rulebase: `server-config/agent-central/docs/sessions.md`
  at the workspace root.
