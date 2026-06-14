# Usage

A personal index of public sex offender registry data, normalized across
jurisdictions. The project has four layers:

1. **Schema** — a Pydantic `OffenderRecord` (see [schema.md](schema.md)).
2. **Storage** — a filesystem tree under `./registry/`. One folder per person,
   one JSON file per record, photos in a sibling `photos/` folder with an
   authoritative manifest. See [storage-layout.md](storage-layout.md).
3. **Adapters** — per-source modules that map a state's raw shape onto the
   canonical schema. Either hand-written (the Hawaii reference) or generated
   by the agent.
4. **Agent** — given a source URL, investigates it and writes an adapter
   module. Pluggable across LLM providers.

---

## Install

Requires Python 3.11+. Pick the provider(s) you want to use:

```sh
pip install -e '.[anthropic]'      # Claude
pip install -e '.[gemini]'         # Google Gemini (free tier available)
pip install -e '.[openai]'         # OpenAI + Ollama / Groq / Cerebras / OpenRouter / GitHub Models
pip install -e '.[all]'            # All three
```

The base install has no LLM SDK — install at least one extra to use the
`build` command.

---

## Credentials and `.env`

Provider credentials and defaults can live in a `.env` file at the project
root (or any ancestor of your working directory). The CLI loads it
automatically on startup — no need to `export` the variables every shell
session.

```sh
cp .env.example .env
# edit .env and uncomment the lines you need
```

The example file lists every variable the project recognizes (provider
preset, model overrides, all credential env vars).

**Shell-set vars win over the file.** If you `export GOOGLE_API_KEY=...` in
the shell, that overrides whatever's in `.env`. Useful for one-off
overrides without touching the file.

Pass `--env-file PATH` if your file is somewhere unusual:

```sh
registry-faces --env-file ./secrets/dev.env build URL --name foo --jurisdiction US-XX
```

`.env` is in `.gitignore`; `.env.example` is committed and serves as the
template. Never commit a real `.env` — and if you do by accident, rotate
those keys immediately.

## Pick a provider

```sh
registry-faces providers
```

prints the available presets. Each preset fixes a default model, base URL,
and credential env var. Override the model with `--model NAME` or set
`REGISTRY_FACES_MODEL` in the environment.

| Preset          | Cost                  | Credential env       | Notes |
|-----------------|-----------------------|----------------------|-------|
| `anthropic`     | Paid                  | `ANTHROPIC_API_KEY`  | Best tool-use + code quality |
| `gemini`        | Free tier             | `GOOGLE_API_KEY`     | Best free option; long context |
| `openai`        | Paid                  | `OPENAI_API_KEY`     | |
| `ollama`        | Free, local           | none                 | Needs `ollama serve` running |
| `groq`          | Free tier             | `GROQ_API_KEY`       | Very fast; Llama 3.3 70B |
| `cerebras`      | Free tier             | `CEREBRAS_API_KEY`   | Fast; daily quota |
| `openrouter`    | Pay-as-you-go + free  | `OPENROUTER_API_KEY` | Aggregator; some `:free` models |
| `github-models` | Free w/ rate limits   | `GITHUB_TOKEN`       | Needs GitHub account |

Set the default for a shell:

```sh
export REGISTRY_FACES_PROVIDER=gemini
export GOOGLE_API_KEY=...
```

Or pass per-invocation:

```sh
registry-faces build https://... --name foo --jurisdiction US-XX --provider ollama
```

### Local with Ollama (no key, no network for the LLM)

```sh
brew install ollama
ollama serve &
ollama pull qwen2.5-coder:32b
export REGISTRY_FACES_PROVIDER=ollama
```

For smaller hardware, try `--model qwen2.5-coder:7b` or
`deepseek-coder-v2:16b` — expect more iteration loops and weaker reasoning
on messy HTML.

---

## Registry root

By default everything goes in `./registry/`. Override globally:

```sh
registry-faces --registry /path/to/registry stats
```

Or with the short flag `-r`. The directory is created on first use.

---

## Build an adapter

The agent investigates a source URL and writes a deterministic adapter to
`adapters_generated/<name>.py`.

```sh
registry-faces build \
    https://hcjdc.ehawaii.gov/bulkcor/public/csv \
    --name hawaii_v2 \
    --jurisdiction US-HI
```

What happens:

1. Agent calls `head_url` / `fetch_url` to classify the source type.
2. Reads the canonical schema and the Hawaii reference adapter.
3. Writes the adapter file with `fetch()`, `normalize()`, and
   `extract_photos()` methods.
4. Runs `test_adapter` to validate 5 records against the schema.
5. Iterates if anything fails (up to 3 cycles).

After that, **Claude is out of the loop** — the adapter is plain Python that
runs deterministically on every ingest.

---

## Ingest records

```sh
registry-faces ingest hawaii
registry-faces ingest hawaii_v2
registry-faces ingest florida
```

**Ingest is idempotent.** Running it again merges new data into existing
records, never deletes anything, and updates timestamps. See
[storage-layout.md](storage-layout.md#merge--idempotency-rules) for the full
merge rules.

Photo URLs published by the source are added to each person's
`photos/manifest.json` as pending entries during ingest — no images are
downloaded yet.

---

## Sync photos

A separate step actually downloads the photos referenced in each person's
manifest:

```sh
registry-faces sync-photos                          # all jurisdictions
registry-faces sync-photos --jurisdiction US-HI
registry-faces sync-photos --refresh                # re-download even if already on disk
```

This is idempotent too — running it again only fetches entries that don't
already have a `local_filename`. It only ever downloads URLs that came from
the source registry payload itself; nothing scraped or inferred.

---

## Verify

```sh
registry-faces verify
```

Walks every person's folder and checks the photo invariant: every file in
`photos/` has exactly one manifest entry, and every entry with a
`local_filename` points to a file that exists. Read-only — never modifies
anything.

---

## Rebuild index

If `indexes/index.jsonl` ever gets corrupted or you want to rebuild it from
the source-of-truth `record.json` files:

```sh
registry-faces rebuild-index
```

---

## Query the store

```sh
registry-faces lookup "Smith" --limit 10
registry-faces near 21.3 -157.85 --radius 5000     # meters
registry-faces stats                                # counts by jurisdiction
```

Radius search uses a bounding-box pre-filter on lat/lon entries in the
index. Records without geocoded addresses won't match a radius query.

---

## Adding adapters manually

The agent is the intended path. If you want to hand-write one, follow
`src/registry_faces/adapters/hawaii.py` as the template:

```python
from registry_faces.adapters.base import Adapter
from registry_faces.schema import OffenderRecord, Source, Identity, Address
from registry_faces.photos import PhotoRef

class MyAdapter(Adapter):
    jurisdiction = "US-XX"
    source_name = "Example State Registry"

    def fetch(self):
        # yield raw dicts
        ...

    def normalize(self, raw):
        return OffenderRecord(
            source=Source(...),
            identity=Identity(full_name=...),
            addresses=[...],
            raw=raw,  # always preserve raw
        )

    def extract_photos(self, raw):
        url = raw.get("photo_url")
        return [PhotoRef(url=url, source_type="registry", source_name=self.source_name)] if url else []

def build():
    return MyAdapter()
```

Drop into `adapters_generated/myname.py` and `registry-faces ingest myname`
picks it up.

---

## Configuration reference

| Variable | Purpose | Example |
|---|---|---|
| `REGISTRY_FACES_PROVIDER` | Default provider preset | `gemini` |
| `REGISTRY_FACES_MODEL`    | Override the preset's model | `gemini-2.0-flash` |
| `REGISTRY_FACES_BASE_URL` | Override the preset's base URL | `http://localhost:11434/v1` |
| `ANTHROPIC_API_KEY` | Claude credential | — |
| `GOOGLE_API_KEY`    | Gemini credential | — |
| `OPENAI_API_KEY`    | OpenAI credential | — |
| `GROQ_API_KEY`      | Groq credential   | — |
| `CEREBRAS_API_KEY`  | Cerebras credential | — |
| `OPENROUTER_API_KEY`| OpenRouter credential | — |
| `GITHUB_TOKEN`      | GitHub Models credential | — |

CLI flags (`--provider`, `--model`, `--registry`) override env vars; env
vars override preset defaults.

---

## What this project will not do

- **Republish data.** It's a personal index. State registry ToS generally
  prohibit secondary databases — this is for your own lookups only.
- **Pull photos from outside the source registry.** `sync-photos` only ever
  downloads URLs that the source itself published in its payload. No image
  search, no news scrape, no social media.
- **Geocoding.** Adapters leave `lat`/`lon` empty unless the source
  provides them. A geocoding pass would slot in between `ingest` and query.
- **Bypass paywalls, captchas, or robot blocks.** The agent will report
  and stop if it hits one.
