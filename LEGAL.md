# Legal & Ethical Notes

This is a **personal tool** for browsing public sex offender registry data,
normalized across jurisdictions. Before you build a new adapter or ingest
data, read this.

> **This is not legal advice.** The notes below are practical heuristics
> from the project's design. State and federal law around access to and
> use of registry data is complex and changes. If you're unsure whether
> something is allowed in your situation, consult an attorney in your
> jurisdiction.

---

## What this project is for

- **Personal lookup** — same use case as visiting [nsopw.gov](https://nsopw.gov)
  or your state's official registry site, but with normalized data and
  local search across multiple jurisdictions.
- **Public-data normalization** — cross-jurisdictional schema so the same
  query works against many sources.

## What this project is NOT for

- **Republishing.** Don't host the data publicly, post it to a server, or
  share it with people who couldn't get it themselves from the original
  source.
- **Facial recognition / identity matching.** Out of scope. The agent is
  instructed not to build adapters that aggregate identifying photos from
  non-registry sources.
- **Harassment, vigilante action, or targeting individuals.** Some states
  criminalize misuse of registry data, including using it to threaten,
  harass, or deny services. Don't.
- **Employment, housing, or credit decisions.** Those trigger the Fair
  Credit Reporting Act (FCRA) and state equivalents — use a licensed
  background-check provider (Sterling, Checkr, etc.), not this tool.

---

## You are responsible for the URL you point the agent at

**The agent will attempt to build an adapter for whatever URL you give it.**
It does not check the site's terms of service, robots.txt, or whether the
site requires login / captcha / payment. If the agent encounters one of
those, it will try to work around it and produce the best adapter it can —
which may put you in violation of the Computer Fraud and Abuse Act (CFAA),
the site's ToS, or both.

This is a deliberate choice in the project's design: the agent is a code
generator, not a policy enforcer. Policy is *your* job — you decide what
URLs are appropriate to point it at.

## State registry Terms of Service vary

Each US state runs its own registry with its own ToS. Common restrictions:

- Prohibitions on **building secondary databases** of the data.
- Prohibitions on **bulk download** or **automated access**.
- Limits on **how the data can be combined** with other sources.
- Requirements that the data **only be used for personal lookup**, not
  republished or sold.

### Before adding an adapter for a new state:

1. Visit the state's registry homepage in a browser.
2. Read their "Terms of Use" or equivalent disclaimer page.
3. Decide whether personal-use local indexing is allowed.
4. If yes, run `registry-faces build <url> --name <state> --jurisdiction US-XX`.
5. **Review the generated Python code in `adapters_generated/<name>.py`**
   before running ingest. Confirm it isn't doing anything you didn't intend.

### States with notable restrictions

- **California (Megan's Law)** — explicitly criminalizes misuse of registry
  data. This project does not include a California adapter. Use the
  [official site](https://www.meganslaw.ca.gov) instead.
- **States with login-gated bulk data** (e.g. Hawaii) — bulk access is
  only for credentialed users with a documented purpose. The Hawaii
  adapter in this project is non-functional by design; see
  [src/registry_faces/adapters/hawaii.py](src/registry_faces/adapters/hawaii.py).

---

## Photos

- Adapters download **only** photos that the source registry's own JSON /
  HTML payload publishes. Period.
- **No image search.** No news article scraping. No social media. The
  agent's `extract_photos()` is structurally limited to URLs the source
  itself returns.
- Some state ToS prohibit local caching of photos. If your state has that
  restriction, don't run `sync-photos` for it — the URLs in
  `photos/manifest.json` remain as references and you can still visit the
  source site directly via the per-record `info_url`.

---

## What the agent will and won't do

The agent will:

- Investigate any URL you point it at — bulk files, JSON APIs, paginated
  HTML, JS-rendered SPAs. It tries to figure out the right access pattern
  and write code that follows it.
- Generate Python that uses `httpx` + `beautifulsoup4` + `lxml` against
  whatever endpoint it identifies.

The agent will *not*:

- **Aggregate photos from outside the source.** No image search, no news
  article scraping, no social media. The agent's `extract_photos()` is
  structurally limited to URLs the source registry's own payload returns.
  This is a hard rule baked into the system prompt and the storage schema.

Everything else — including whether the site permits the access pattern —
is on you. See **"You are responsible for the URL you point the agent at"**
above.

---

## What you should review before running an agent-generated adapter

After the Builder tab finishes producing `adapters_generated/<name>.py`,
**open the file and read it**. Specifically check:

- The URLs it fetches — make sure they're only on the source's domain.
- The field mappings — make sure it isn't pulling in unexpected fields.
- The `extract_photos()` method — make sure photo URLs are from the source.
- That `fetch()` doesn't issue large numbers of requests in a tight loop
  (rate-limit politeness is the right default).

If anything looks wrong, delete the adapter and either re-run the agent
with a more directive prompt, or write the adapter by hand using
`src/registry_faces/adapters/south_dakota.py` as a template.

---

## Reporting issues

If you spot an adapter doing something off-spec, or you discover a way to
misuse this tool that the docs don't address, fix the adapter (delete or
rewrite) and update this file with what you learned.
