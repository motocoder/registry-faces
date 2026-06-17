# registry-faces

A personal index over public US sex offender registry data, normalized
across jurisdictions, with an agent that writes per-source ingest adapters
on demand.

**Read [`LEGAL.md`](LEGAL.md) before generating or running any adapter.**
The desktop app blocks first launch until you've acknowledged it.

---

## What it does

- **Normalizes** per-state registry data onto one canonical schema
  ([`docs/schema.md`](docs/schema.md)).
- **Stores** records as plain JSON files in a per-person folder tree —
  no SQL, browse it in Finder ([`docs/storage-layout.md`](docs/storage-layout.md)).
- **Indexes** for fast name / ZIP / geo-radius search.
- **Builds new adapters** by giving an LLM agent a source URL and letting
  it generate Python code, which then runs deterministically. The agent
  pluggable across 8 providers — including a fully-local one (Ollama) that
  needs no API key.
- **Caches photos** that the source registry's own payload publishes — no
  scraping outside the source, no image search, no social media.

---

## Install

Requires Python 3.11+.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[all]'    # all LLM providers + desktop UI
```

For a leaner install, pick what you need:

| Extra | Pulls in |
|---|---|
| `[anthropic]` | Claude (paid) |
| `[gemini]` | Google Gemini (free tier available) |
| `[openai]` | OpenAI + every OpenAI-compatible provider (Ollama, Groq, Cerebras, OpenRouter, GitHub Models) |
| `[ui]` | PySide6 (desktop app) |
| `[dev]` | pytest (run the test suite) |
| `[all]` | Everything above |

---

## Launch

### Desktop app

```sh
./run.sh
```

Three tabs:
- **Search** — name / ZIP / geo-radius queries, with photo gallery + "Open
  on source website" link per result.
- **Scripts** — list of in-package + generated adapters; per-row buttons
  for Ingest / Sync Photos / Verify / Open Source / Delete.
- **Builder** — provider dropdown + base URL → agent writes a deterministic
  Python adapter and tests it. Live log streams from the agent.

### CLI

```sh
registry-faces stats                          # counts by jurisdiction
registry-faces lookup SMITH                   # name substring
registry-faces near 43.5444 -96.7341 --radius 1609
registry-faces ingest south_dakota            # idempotent merge
registry-faces sync-photos                    # downloads pending photos
registry-faces build URL --name foo --jurisdiction US-XX --provider gemini
registry-faces ui                             # opens the desktop app
```

`registry-faces --help` for the full list. Every command accepts
`--registry PATH` to point at a registry root other than `./registry/`.

---

## Configure providers

```sh
cp .env.example .env
# edit .env, uncomment and set the keys you actually use
```

The CLI auto-loads `.env` on startup. Set `REGISTRY_FACES_PROVIDER` to
pick which preset is the default. See [`docs/usage.md`](docs/usage.md) for
the full list of providers and how the agent uses them.

**No keys at all?** Use `--provider ollama` and run a local model:

```sh
ollama pull qwen2.5:14b
./run.sh    # or: registry-faces build URL --name foo --jurisdiction US-XX --provider ollama --model qwen2.5:14b
```

---

## Project layout

```
registry-faces/
├── LEGAL.md                   ← READ THIS FIRST
├── docs/
│   ├── usage.md               ← install + every CLI command
│   ├── schema.md              ← OffenderRecord field reference
│   └── storage-layout.md      ← filesystem layout + merge rules
├── src/registry_faces/
│   ├── schema.py              ← Pydantic OffenderRecord
│   ├── store.py               ← FileStore (per-person folders, merge logic)
│   ├── photos.py              ← PhotoRef / PhotoManifest / sync_photos
│   ├── adapters/              ← 30 in-package state adapters (hawaii = reference)
│   ├── agent/                 ← provider abstraction + agent builder
│   └── desktop/               ← PySide6 UI (three tabs + workers)
├── adapters_generated/        ← agent-produced adapters land here
├── assets/icon.png            ← app icon (replaceable)
├── tests/                     ← pytest tests
└── registry/                  ← your data lives here (gitignored)
```

---

## Run the tests

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The test suite covers the merge invariants in `store.py`, the photo
manifest contract in `photos.py`, and the tool-schema introspection.
Adapters and the UI are smoke-tested through their factory functions.

---

## What is and isn't in scope

In scope: indexing public registry data the source itself publishes;
caching the source's own photos; per-record search and lookup.

Out of scope: republishing the data publicly; facial recognition; image
search outside the source; using the data for employment / housing /
credit decisions; targeting individuals.

Full details and the legal framing are in [`LEGAL.md`](LEGAL.md).
