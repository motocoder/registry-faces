"""CLI entry point.

  registry-faces providers
  registry-faces build <url> --name florida --jurisdiction US-FL [--provider gemini]
  registry-faces ingest hawaii
  registry-faces sync-photos [--jurisdiction US-HI] [--refresh]
  registry-faces verify
  registry-faces rebuild-index
  registry-faces backfill-guids
  registry-faces lookup "Smith"
  registry-faces near 21.3 -157.8 --radius 1609
  registry-faces stats

Global option:
  --registry PATH    Registry root directory (default: ./registry)
"""

from __future__ import annotations

import os
from pathlib import Path

import click
from dotenv import load_dotenv
from web_scrubber.discovery import AdapterNotFound, load_adapter as _ws_load_adapter

from .agent.builder import build_adapter_from_url, resolve_mode
from .agent.providers import PRESETS, list_presets
from .photos import iter_person_dirs, sync_photos, verify_person_photos
from .store import FileStore

ADAPTERS_OUT = Path("adapters_generated")

# Canary fields for ingest health: if a registry's format changes, one of these
# usually stops parsing and its coverage collapses (or the run yields 0 records
# / raises). ``r`` is the OffenderRecord, ``ph`` its photos.
REGISTRY_FIELD_CHECKS = [
    ("name", lambda r, ph: bool(r.identity.full_name)),
    ("offenses", lambda r, ph: len(r.offenses) > 0),
    ("sex", lambda r, ph: r.identity.sex != "unknown"),
    ("photo", lambda r, ph: len(ph) > 0),
]


def _load_adapter(name: str):
    try:
        return _ws_load_adapter(name, "registry_faces.adapters", ADAPTERS_OUT)
    except AdapterNotFound as e:
        raise click.ClickException(str(e)) from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e


def _load_env_file() -> Path | None:
    """Load .env from the current working directory or its ancestors.

    Shell-set env vars always win (`override=False`) — the file is just a
    convenient default. Returns the path that was loaded, or None.
    """
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        env_path = candidate / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            return env_path
    return None


@click.group()
@click.option(
    "--registry",
    "-r",
    "registry_root",
    default="./registry",
    type=click.Path(file_okay=False),
    show_default=True,
    help="Registry root directory.",
)
@click.option(
    "--env-file",
    "env_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Path to a .env file. Default: auto-discover .env in CWD or parents.",
)
@click.pass_context
def cli(ctx: click.Context, registry_root: str, env_file: str | None) -> None:
    """Personal sex offender registry index with agentic adapter generation."""
    # Load environment from .env unless caller already set variables in the shell.
    if env_file:
        load_dotenv(env_file, override=False)
        ctx.ensure_object(dict)
        ctx.obj["env_file"] = Path(env_file).resolve()
    else:
        loaded_from = _load_env_file()
        ctx.ensure_object(dict)
        ctx.obj["env_file"] = loaded_from
    ctx.obj["registry_root"] = Path(registry_root)
    # Committed default (free-tier) Geocodio key so the nightly run works with
    # zero per-machine setup. A shell var or .env entry still overrides it.
    # Intentionally checked in — it's a free key with a 2,500/day cap; rotate it
    # if this repo goes public or the key ever gets billing attached.
    os.environ.setdefault("GEOCODIO_API_KEY", "6645faa6711185475c2756f3cf6a66c85468516")


# ---------------------------------------------------------------------------
# Providers


@cli.command()
def providers() -> None:
    """List available LLM provider presets."""
    click.echo("Available providers (set with --provider or REGISTRY_FACES_PROVIDER):\n")
    for name, cfg in PRESETS.items():
        key_env = cfg.get("api_key_env") or "(no key needed)"
        url = cfg.get("base_url", "—")
        click.echo(f"  {name:14}  model={cfg['model']:<35}  key={key_env}")
        if url != "—":
            click.echo(f"  {'':14}  base_url={url}")
    click.echo(
        "\nOverride defaults: --model NAME, --base-url URL, or env vars "
        "REGISTRY_FACES_MODEL / REGISTRY_FACES_BASE_URL."
    )


# ---------------------------------------------------------------------------
# Build (agent)


@cli.command()
@click.argument("url")
@click.option("--name", required=True, help="Adapter name, e.g. 'florida'.")
@click.option("--jurisdiction", required=True, help="Jurisdiction code, e.g. 'US-FL'.")
@click.option(
    "--provider",
    type=click.Choice(list_presets(), case_sensitive=False),
    default=None,
    help="LLM provider preset. Default: $REGISTRY_FACES_PROVIDER or 'anthropic'.",
)
@click.option("--model", default=None, help="Override the provider's default model.")
@click.option(
    "--mode",
    type=click.Choice(["auto", "create", "update"], case_sensitive=False),
    default="auto",
    show_default=True,
    help=(
        "auto: update if adapter exists, else create. "
        "create: always write from scratch. "
        "update: review the existing adapter and make minimal targeted edits."
    ),
)
def build(
    url: str,
    name: str,
    jurisdiction: str,
    provider: str | None,
    model: str | None,
    mode: str,
) -> None:
    """Use the agent to build or update an adapter from a source URL."""
    resolved = resolve_mode(mode, name)  # type: ignore[arg-type]
    click.echo(
        f"Adapter '{name}' from {url} "
        f"(provider={provider or 'default'}, mode={mode} -> {resolved}) ..."
    )
    report = build_adapter_from_url(
        url, name, jurisdiction, provider=provider, model=model, mode=mode  # type: ignore[arg-type]
    )
    click.echo(report)


@cli.command("expand")
@click.option("--regions", default=None, help="path to an expand_countries.toml (countries/pacing).")
@click.option("--engine", default=None, help="agent engine: claude (default) or a provider preset.")
@click.option("--model", default=None, help="override the agent model.")
@click.option("--country", default=None, help="restrict to one ISO-3166 alpha-2 code.")
@click.option("--delay", type=float, default=None, help="seconds between iterations.")
@click.option("--max-per-day", "max_per_day", type=int, default=None,
              help="cap successful additions per day (0 = unlimited).")
@click.option("--once", is_flag=True, help="run a single iteration then exit.")
@click.option("--dry-run", "dry_run", is_flag=True, help="scout + verify but make no ledger write.")
def expand(regions, engine, model, country, delay, max_per_day, once, dry_run) -> None:
    """Continuously discover + build per-country sex-offender-registry adapters (LLM agent).

    Walks every ISO-3166 country in docs/country_coverage.csv: asks the agent to find
    a PUBLIC registry and write an adapter, verifies records, and records covered /
    unsupported (most countries are unsupported — public registries are rare).
    """
    from types import SimpleNamespace

    from .expand import run_expand

    raise SystemExit(run_expand(SimpleNamespace(
        regions=regions, engine=engine, model=model, country=country,
        delay=delay, max_per_day=max_per_day, once=once, dry_run=dry_run)))


# ---------------------------------------------------------------------------
# Ingest


@cli.command()
@click.argument("name")
@click.pass_context
def ingest(ctx: click.Context, name: str) -> None:
    """Run an adapter, merge its records into the store, and health-check the run.

    Compares the run against the adapter's last-good baseline (record count +
    field coverage of name/offenses/sex/photo) and exits non-zero on a likely
    format break — 0 records, a severe count drop, a run-level exception, or a
    normally-present field whose parsing collapsed. See also `health`.
    """
    from web_scrubber.health import ingest_with_health

    adapter = _load_adapter(name)
    root = ctx.obj["registry_root"]
    health_path = Path(root) / "indexes" / "health.json"
    with FileStore(root) as store:
        result = ingest_with_health(
            adapter,
            store,
            adapter_name=name,
            jurisdiction=getattr(adapter, "jurisdiction", ""),
            field_checks=REGISTRY_FIELD_CHECKS,
            health_path=health_path,
            on_progress=lambda n: click.echo(f"  ... {n}"),
        )
    click.echo(result.report())
    if result.failed:
        raise click.ClickException(
            f"ingest health check FAILED for {name} — likely a source format change."
        )


# ---------------------------------------------------------------------------
# Sync photos


@cli.command("sync-photos")
@click.option("--jurisdiction", default=None, help="Restrict to one jurisdiction.")
@click.option("--refresh", is_flag=True, help="Re-download even entries already on disk.")
@click.pass_context
def sync_photos_cmd(ctx: click.Context, jurisdiction: str | None, refresh: bool) -> None:
    """Download any pending photo entries from each person's manifest."""
    root = ctx.obj["registry_root"]
    records_root = root / "records"
    if not records_root.exists():
        click.echo("No records yet. Run `ingest` first.")
        return
    summary = sync_photos(records_root, jurisdiction=jurisdiction, refresh=refresh)
    click.echo(
        f"Downloaded: {summary['downloaded']}  Skipped: {summary['skipped']}  "
        f"Failed: {len(summary['failed'])}"
    )
    for url, err in summary["failed"][:20]:
        click.echo(f"  FAIL {url}: {err}")
    if len(summary["failed"]) > 20:
        click.echo(f"  ... and {len(summary['failed']) - 20} more failures")


# ---------------------------------------------------------------------------
# Verify


@cli.command()
@click.pass_context
def verify(ctx: click.Context) -> None:
    """Check that every photo file has a manifest entry, and vice versa."""
    root = ctx.obj["registry_root"]
    records_root = root / "records"
    if not records_root.exists():
        click.echo("No records yet.")
        return
    total_issues = 0
    for person_dir in iter_person_dirs(records_root):
        issues = verify_person_photos(person_dir)
        for issue in issues:
            click.echo(issue)
            total_issues += 1
    if total_issues == 0:
        click.echo("All photo manifests are consistent.")
    else:
        click.echo(f"\n{total_issues} issue(s) found.")


# ---------------------------------------------------------------------------
# Rebuild index


@cli.command("rebuild-index")
@click.pass_context
def rebuild_index(ctx: click.Context) -> None:
    """Walk records/ and regenerate indexes/index.jsonl."""
    root = ctx.obj["registry_root"]
    with FileStore(root) as store:
        count = store.rebuild_index()
    click.echo(f"Rebuilt index. {count} records.")


@cli.command("backfill-guids")
@click.pass_context
def backfill_guids(ctx: click.Context) -> None:
    """Assign a stable identity.guid to any record that lacks one on disk."""
    root = ctx.obj["registry_root"]
    with FileStore(root) as store:
        updated = store.backfill_guids()
    click.echo(f"Backfilled guid on {updated} record(s).")


# ---------------------------------------------------------------------------
# Lookup / near / stats


@cli.command()
@click.argument("query")
@click.option("--limit", type=int, default=20)
@click.pass_context
def lookup(ctx: click.Context, query: str, limit: int) -> None:
    """Search by name substring."""
    root = ctx.obj["registry_root"]
    with FileStore(root) as store:
        results = store.search_name(query, limit=limit)
        for r in results:
            addrs = (
                "; ".join(
                    f"{a.street or ''} {a.city or ''}, {a.state or ''} {a.zip or ''}".strip()
                    for a in r.addresses
                )
                or "(no addresses)"
            )
            click.echo(f"[{r.source.jurisdiction}] {r.identity.full_name} — {addrs}")
    click.echo(f"\n{len(results)} match(es).")


@cli.command()
@click.argument("lat", type=float)
@click.argument("lon", type=float)
@click.option("--radius", type=float, default=1609, help="Radius in meters (default 1609 = 1mi).")
@click.pass_context
def near(ctx: click.Context, lat: float, lon: float, radius: float) -> None:
    """Search by lat/lon radius (meters)."""
    root = ctx.obj["registry_root"]
    with FileStore(root) as store:
        results = store.search_radius(lat, lon, radius)
        for r in results:
            cities = "; ".join(
                f"{a.city or '?'}, {a.state or '?'}" for a in r.addresses if a.lat is not None
            )
            click.echo(f"[{r.source.jurisdiction}] {r.identity.full_name} — {cities}")
    click.echo(f"\n{len(results)} within {radius:.0f}m of ({lat}, {lon}).")


@cli.command()
@click.pass_context
def ui(ctx: click.Context) -> None:
    """Launch the desktop UI."""
    try:
        from .desktop.app import main as ui_main
    except ImportError as e:
        raise click.ClickException(
            "PySide6 is not installed. Install with: pip install 'registry-faces[ui]'"
        ) from e
    ui_main(
        registry_root=ctx.obj["registry_root"],
        env_file=ctx.obj.get("env_file"),
    )


@cli.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show record counts by jurisdiction."""
    root = ctx.obj["registry_root"]
    with FileStore(root) as store:
        counts = store.stats()
        if not counts:
            click.echo("Store is empty.")
            return
        for jur, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            click.echo(f"  {jur:>10}: {n:>6}")
        click.echo(f"  {'TOTAL':>10}: {store.count():>6}")


class _StoreBackedAdapter:
    """Replays already-ingested local records as adapter items for --from-store.

    Yields the ``(OffenderRecord, [PhotoRef])`` shape ``map_item`` expects, so the
    whole local corpus can be pushed into the identity service without re-fetching
    every live source. Photo refs come from each record's saved manifest.
    """

    def __init__(self, store: "FileStore") -> None:
        self._store = store

    def run(self):
        from web_scrubber.photos import read_manifest

        for rec in self._store.filter():
            person_dir = self._store.person_dir(rec.source.jurisdiction, rec.source.source_id)
            manifest = read_manifest(person_dir)
            yield rec, (manifest.photos if manifest else [])


@cli.command("ingest-identity")
@click.argument("name", required=False)
@click.option(
    "--from-store",
    "from_store",
    is_flag=True,
    help="Replay ALL already-ingested local records into identity (no re-fetch) "
    "instead of running one live adapter.",
)
@click.option(
    "--to",
    "target",
    type=click.Choice(["file", "hbase"]),
    default=None,
    help="Override identity.mode (file = dry-run to local files, hbase = production).",
)
@click.option(
    "--config",
    "config_path",
    default="identity.properties",
    type=click.Path(dir_okay=False),
    show_default=True,
    help="Identity backend properties file (connectivity for HBase / dry-run root).",
)
@click.option(
    "--bulk",
    is_flag=True,
    help="Fast path for big states: GATHER all records first (network, no lock — "
    "run many states in parallel), THEN briefly take the global lock and bulk-"
    "upload. Lock is held only for the upload, not the fetch.",
)
@click.option(
    "--force-unlock",
    "force_unlock",
    is_flag=True,
    help="Clear a stale ingest lock left by a crashed run, then proceed. Use only "
    "when you are certain no other run for the same lock is active.",
)
@click.pass_context
def ingest_identity(
    ctx: click.Context, name: str | None, from_store: bool, target: str | None,
    config_path: str, bulk: bool, force_unlock: bool,
) -> None:
    """Run an adapter (or replay the local store) into the centralized person identity.

    Either run one live adapter (``ingest-identity texas``) or replay everything
    already on disk (``ingest-identity --from-store``). Dry-run to local files by
    default; pass --to hbase (or set identity.mode=hbase) for production.

    HBase strategies:

    \b
    * default, single state — PER-STATE lock (``registry:<jurisdiction>``) held
      for the whole run; several states run fully in parallel.
    * --bulk (or --from-store) — gather records first with NO lock, then take the
      GLOBAL lock just long enough to bulk-upload. Best for large states / the
      whole corpus; uploads serialize, fetches don't.
    """
    from web_scrubber.person.config import build_identity_service, load_config
    from web_scrubber.person.hbase import IngestLockError, prepare_ingest
    from web_scrubber.person.ingest import ingest_adapter, ingest_items

    from .identity_map import map_item

    if from_store == bool(name):
        raise click.ClickException("Pass either an adapter NAME or --from-store (exactly one).")

    cfg = load_config(config_path, mode_override=target)
    store = None
    if from_store:
        store = FileStore(ctx.obj["registry_root"])
        adapter: object = _StoreBackedAdapter(store)
        label, lock_key, lock_owner = "ALL local records", "identity", "registry-faces:from-store"
    else:
        adapter = _load_adapter(name)
        label = f"{name} ({adapter.jurisdiction})"
        # --bulk spans the upload under the global lock; otherwise per-state lock.
        lock_key = "identity" if bulk else f"registry:{adapter.jurisdiction}"
        lock_owner = f"registry-faces:{name}"

    # One rebuildable bundle so the ingest loop can reconnect after a dropped
    # connection (a long scrape can idle the gateway past its read timeout).
    _holder: dict = {}

    def _open(**bk):
        _holder["b"] = build_identity_service(cfg, **bk)
        return _holder["b"].service

    def _reopen(**bk):
        try:
            _holder["b"].close()
        except Exception:  # noqa: BLE001
            pass
        return _open(**{**bk, "force_unlock": True})

    try:
        if bulk and cfg.mode == "hbase":
            # Phase 1: gather + map every record with NO lock held (parallel-safe).
            click.echo(f"Identity ingest [bulk]: {label} -> hbase")
            click.echo("  phase 1/2: gathering records (no lock held)...")
            triples = []
            for item in adapter.run():
                person, att, ph = map_item(item)
                # serialize the new-person payload here, off the lock
                triples.append((person, att, ph, prepare_ingest(person, att)))
                if len(triples) % 1000 == 0:
                    click.echo(f"    gathered {len(triples)}")
            click.echo(f"  gathered {len(triples)} records")
            # Phase 2: take the GLOBAL lock (bulk store needs sole writer) + upload.
            click.echo("  phase 2/2: acquiring global lock + bulk upload...")
            bk = dict(bulk=True, force_unlock=force_unlock,
                      lock_owner=f"{lock_owner}:bulk", lock_key="identity")
            stats = ingest_items(
                _open(**bk), triples, reopen=lambda: _reopen(**bk),
                on_progress=lambda n: click.echo(f"    ... {n}"),
            )
        else:
            click.echo(f"Identity ingest: {label} -> {cfg.mode} [lock={lock_key}]")
            bk = dict(force_unlock=force_unlock, lock_owner=lock_owner, lock_key=lock_key)
            stats = ingest_adapter(
                _open(**bk), adapter, map_item, reopen=lambda: _reopen(**bk),
                on_progress=lambda n: click.echo(f"  ... {n}"),
            )
        click.echo(stats.line())
    except IngestLockError as e:
        raise click.ClickException(str(e)) from e
    finally:
        if _holder.get("b") is not None:
            try:
                _holder["b"].close()
            except Exception:  # noqa: BLE001
                pass
        if store is not None:
            store.close()


@cli.command("sync-identity-photos")
@click.option("--to", "target", type=click.Choice(["file", "hbase"]), default=None)
@click.option("--config", "config_path", default="identity.properties", type=click.Path(dir_okay=False), show_default=True)
@click.option("--refresh", is_flag=True, help="Re-download already-fetched photos.")
@click.option("--pause", "pause_seconds", default=0.25, type=float, show_default=True)
def sync_identity_photos(target: str | None, config_path: str, refresh: bool, pause_seconds: float) -> None:
    """Download photo bytes for the centralized person store into its BlobStore."""
    from web_scrubber.person.config import build_identity_service, load_config

    photo_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    cfg = load_config(config_path, mode_override=target)
    click.echo(f"Identity photo sync -> {cfg.mode}")
    with build_identity_service(cfg, lock_owner="registry-faces", lock_key="photo:registry-faces") as bundle:
        stats = bundle.sync_photos(refresh=refresh, user_agent=photo_ua, pause_seconds=pause_seconds)
    click.echo(stats.line())
    for url, err in stats.failed[:10]:
        click.echo(f"  FAIL {url} -- {err}")


@cli.command("enrich-details")
@click.option("--to", "target", type=click.Choice(["file", "hbase"]), default=None)
@click.option("--config", "config_path", default="identity.properties",
              type=click.Path(dir_okay=False), show_default=True)
@click.option("--limit", type=int, default=None,
              help="Stop after N persons scanned (for testing a slice).")
@click.option("--pause", "pause_seconds", default=0.5, type=float, show_default=True,
              help="Delay between detail-page fetches (politeness/rate-limit).")
@click.option("--kind", default="registry", show_default=True,
              help="Only enrich persons with an attachment of this kind "
                   "(registry/wanted/missing/booking). Scopes the backfill.")
@click.option("--shard", default=None,
              help="Process only a hash-slice 'i/N' of persons, so N processes "
                   "can run in parallel (e.g. --shard 0/20 ... --shard 19/20).")
@click.option("--headless/--headed", "headless", default=True, show_default=True,
              help="Run Chromium headless (no on-screen windows). Headless is the "
                   "right default for bulk state-registry pages; use --headed only "
                   "for sites whose bot gate defeats headless (rare here).")
def enrich_details(target: str | None, config_path: str, limit: int | None,
                   pause_seconds: float, kind: str, shard: str | None,
                   headless: bool) -> None:
    """Backfill physical specs by fetching each person's detail page.

    Fills ONLY missing race / eye / hair / height / weight / age fields (the
    aggregator APIs omit them — they live on the per-offender detail page), via
    the shared, deterministic detail extractor. Idempotent and safe to re-run;
    skips persons that already have the data. Detail URLs come from each
    person's attachments (info_url / source_url / offenderUri).
    """
    import time
    from datetime import datetime, timezone

    from web_scrubber.fetch.browser import BrowserFetcher
    from web_scrubber.person.config import build_identity_service, load_config
    from web_scrubber.person.detail_enrich import run_enrichment

    cfg = load_config(config_path, mode_override=target)
    year = datetime.now(timezone.utc).year
    shard_t = None
    if shard:
        i, n = (int(x) for x in shard.split("/"))
        shard_t = (i, n)
    click.echo(f"Detail enrichment -> {cfg.mode} (reference year {year}, "
               f"kind={kind}, shard={shard or 'all'})")
    # lock=False: an idempotent backfill that only fills gaps — don't contend
    # with an in-progress ingest's single-writer lock.
    with build_identity_service(cfg, lock=False) as bundle, \
            BrowserFetcher(headless=headless) as fetcher:
        def fetch_html(url: str) -> str | None:
            time.sleep(pause_seconds)
            try:
                return fetcher.fetch(url)
            except Exception:  # noqa: BLE001 — a bad page never aborts the run
                return None

        scanned, enriched = run_enrichment(
            bundle.store, fetch_html, reference_year=year, limit=limit, kinds=(kind,),
            shard=shard_t,
            on_progress=lambda s, e: click.echo(f"  scanned={s} enriched={e}"),
        )
    click.echo(f"done: scanned={scanned} enriched={enriched}")


@cli.command("regeocode-hbase")
@click.option("--config", "config_path", default="identity.properties",
              type=click.Path(dir_okay=False), show_default=True)
@click.option("--dry-run", is_flag=True, help="Report what would change without writing.")
@click.option("--force-unlock", "force_unlock", is_flag=True,
              help="Clear a stale ingest lock before acquiring (use when a prior run crashed).")
@click.option("--limit", type=int, default=0,
              help="Stop after ~N persons (0 = whole table). For dry-run validation.")
@click.option("--census-only", "census_only", is_flag=True,
              help="Free run: skip the paid Geocodio tier (Census street + city/offline best-guess only).")
@click.option("--cursor-file", "cursor_file", default=None,
              help="Resume cursor file (default: <file_root>/regeocode-cursor.txt). Cleared on a full sweep.")
def regeocode_hbase(
    config_path: str, dry_run: bool, force_unlock: bool, limit: int = 0,
    census_only: bool = False, cursor_file: str | None = None,
) -> None:
    """Street-geocode REGISTRY attachments in HBase (Census→Geocodio, offline fallback).

    Confirms each registry address to a SPECIFIC point where one exists: the
    Census batch geocoder (free, US street-level) then Geocodio (misses +
    territories) resolve real street coordinates (geo_precision rooftop/street,
    or a small-town centroid = city). Whatever can't be street-confirmed falls
    back to the offline disperser (state bbox / zip-city radius / random) — those
    coarse rows are what the map keep-rule then drops. Genuine source coords
    (exact) are never overwritten. Bumps i:json so the map delta-pull picks it up.
    Takes the global ingest lock; re-run if held.

    Set GEOCODIO_API_KEY (via .env) to enable the Geocodio tier; without it the
    run is Census-only.
    """
    import json
    import os
    import time
    from collections import Counter
    from pathlib import Path

    import happybase

    from web_scrubber.geocode import disperse_in_radius, geocode_address, state_from_jurisdiction
    from web_scrubber.hbase_scan import iter_scan
    from web_scrubber.small_places import city_centroid
    from web_scrubber.street_geocode import street_geocode
    from web_scrubber.person.config import load_config
    from web_scrubber.person.hbase import IngestLock
    from web_scrubber.person.hbase import force_unlock as _force_unlock

    cfg = load_config(config_path)
    conn = happybase.Connection(
        host=cfg.hbase_host, port=cfg.hbase_port,
        table_prefix=cfg.hbase_table_prefix or None,
    )
    if force_unlock:
        msg = _force_unlock(conn)
        click.echo(f"force-unlock: {msg}")
    person = conn.table("person")
    # Resume across tunnel-drop crashes: the cursor is the last row written; a
    # re-run continues after it (re-geocoding is idempotent, so overlap is safe).
    cursor_path = Path(cursor_file) if cursor_file else Path(cfg.file_root) / "regeocode-cursor.txt"
    prev_cursor = cursor_path.read_text().strip() if cursor_path.exists() else ""
    start_row = prev_cursor.encode("utf-8") if prev_cursor else None
    if start_row:
        click.echo(f"resuming after cursor: {prev_cursor}")
    lock = IngestLock(conn, owner="registry-faces:regeocode-hbase").acquire()
    click.echo(f"lock acquired; scanning person table (dry_run={dry_run}) ...")
    prec: Counter = Counter()
    n_person = n_att = n_addr = 0
    geocodio_key = None if census_only else os.environ.get("GEOCODIO_API_KEY")
    click.echo(f"geocodio fallback: {'enabled' if geocodio_key else 'DISABLED (free: census + city/offline)'}")

    # Street-first geocoding runs in BATCHES (Census accepts ≤10k/request), so we
    # buffer non-exact registry addresses across rows, resolve a batch through the
    # Census→Geocodio cascade, and fall back to the offline disperser for whatever
    # can't be street-confirmed. A row's addresses are fully appended before the
    # size check, so an attachment is never split across a flush (its JSON is
    # rewritten whole). Idempotent: a re-seen row re-geocodes to the same result.
    BATCH = 4000
    addr_buf: list[tuple] = []  # (att, addr_dict, jur, sid, index)
    row_buf: dict[bytes, dict] = {}  # ukey -> {"ij": bytes|None, "cols": {col: att}}

    def flush() -> None:
        nonlocal n_person, n_att
        if not addr_buf:
            return
        cands = {
            str(idx): {"street": a.get("street"), "city": a.get("city"),
                       "state": a.get("state"), "zip": a.get("zip")}
            for idx, (_att, a, _j, _s, _i) in enumerate(addr_buf)
            if a.get("street") and a.get("state")
        }
        hits = street_geocode(cands, geocodio_key=geocodio_key) if cands else {}
        touched: set[int] = set()
        for idx, (att, a, jur, sid, i) in enumerate(addr_buf):
            r = hits.get(str(idx))
            if r is not None:
                a["lat"], a["lon"], a["geo_precision"] = r.lat, r.lon, r.source
            else:
                # Free best-guess for a real address we couldn't street-confirm:
                # the CITY centroid (jittered so a city's registrants don't stack
                # on one point), far better than a random in-state scatter. Only
                # the truly city-less fall through to the offline state disperser.
                cc = city_centroid(a.get("state"), a.get("city"))
                if cc is not None:
                    dlat, dlon = disperse_in_radius(cc[0], cc[1], f"{jur}:{sid}:{i}")
                    a["lat"], a["lon"], a["geo_precision"] = dlat, dlon, "city"
                else:
                    res = geocode_address(
                        seed=f"{jur}:{sid}:{i}", state=a.get("state"), city=a.get("city"),
                        country=a.get("country"), fallback_state=state_from_jurisdiction(jur),
                    )
                    a["lat"], a["lon"], a["geo_precision"] = res.lat, res.lon, res.source
            prec[a["geo_precision"]] += 1
            touched.add(id(att))
        n_att += len(touched)
        if not dry_run and row_buf:
            for attempt in range(4):
                try:
                    for ukey, info in row_buf.items():
                        updates = {col.encode("utf-8"): json.dumps(att).encode("utf-8")
                                   for col, att in info["cols"].items()}
                        if info["ij"]:
                            updates[b"i:json"] = info["ij"]  # bump ts for map delta-pull
                        person.put(ukey, updates)
                    break
                except Exception as e:  # tunnel blip mid-write -> reconnect + retry (idempotent)
                    if attempt == 3:
                        raise
                    click.echo(f"  ! write failed ({type(e).__name__}); reconnecting + retrying")
                    time.sleep(3)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn.open()
            cursor_path.parent.mkdir(parents=True, exist_ok=True)
            cursor_path.write_text(max(row_buf).decode("utf-8"))  # resume point
        n_person += len(row_buf)
        addr_buf.clear()
        row_buf.clear()
        click.echo(f"  ... {n_person} persons")

    completed_sweep = False
    try:
        for ukey, row in iter_scan(person, conn, columns=[b"a", b"i"], row_start=start_row,
                                   batch_size=200, max_restarts=20):
            ij = row.get(b"i:json")
            for col, val in list(row.items()):
                c = col.decode()
                if not c.startswith("a:criminal:registry:"):
                    continue
                att = json.loads(val)
                src = att.get("source", {}) or {}
                jur, sid = src.get("jurisdiction", ""), src.get("source_id", "")
                staged = False
                for i, a in enumerate(att.get("addresses") or []):
                    n_addr += 1
                    if a.get("geo_precision") == "exact" and a.get("lat") is not None:
                        prec["exact"] += 1  # genuine source coord — never overwritten
                        continue
                    addr_buf.append((att, a, jur, sid, i))
                    staged = True
                if staged:
                    row_buf.setdefault(ukey, {"ij": ij, "cols": {}})["cols"][c] = att
            if len(addr_buf) >= BATCH:
                flush()
            if limit and (n_person + len(row_buf)) >= limit:
                break
        else:
            completed_sweep = True  # loop exhausted (not limit-broken) -> whole table done
        flush()
        if completed_sweep and not dry_run and cursor_path.exists():
            cursor_path.unlink()  # full sweep -> reset the resume cursor for next time
    finally:
        lock.release()
    click.echo(f"persons updated={n_person} attachments={n_att} addresses={n_addr}")
    if completed_sweep:
        click.echo("REGEOCODE_COMPLETE")
    for p, c in prec.most_common():
        click.echo(f"  {p:10s} {c}")


@cli.command("upgrade-geocode-hbase")
@click.option("--config", "config_path", default="identity.properties",
              type=click.Path(dir_okay=False), show_default=True)
@click.option("--dry-run", is_flag=True, help="Report what would upgrade without writing.")
@click.option("--limit", type=int, default=2400, show_default=True,
              help="Max Geocodio lookups this run (free tier = 2500/day; stay under it).")
@click.option("--cursor-file", "cursor_file", default=None,
              help="Cursor state file (default: <file_root>/geocode-upgrade-cursor.txt).")
def upgrade_geocode_hbase(
    config_path: str, dry_run: bool, limit: int = 2400, cursor_file: str | None = None
) -> None:
    """Free-tier Geocodio upgrade of best-guess registry pins to real street coords.

    Promotes up to LIMIT ``city``/``state`` (best-guess) registry addresses that
    carry a real street to a Geocodio ``rooftop``/``street`` match. Cursor-paged
    so successive nightly runs cycle through the whole table, staying inside
    Geocodio's 2,500/day FREE allowance — the ~180k backlog upgrades itself over
    ~2-3 months at zero cost. Only actual street matches are applied; a Geocodio
    miss leaves the pin at its best-guess. Needs GEOCODIO_API_KEY (via .env).
    """
    import json
    import os
    from pathlib import Path

    import happybase

    from web_scrubber.geocodio_geocoder import GeocodioGeocoder
    from web_scrubber.hbase_scan import iter_scan
    from web_scrubber.person.config import load_config
    from web_scrubber.person.hbase import IngestLock

    geo = GeocodioGeocoder(os.environ.get("GEOCODIO_API_KEY"))
    if not geo.enabled:
        click.echo("GEOCODIO_API_KEY not set — nothing to do")
        return

    cfg = load_config(config_path)
    conn = happybase.Connection(
        host=cfg.hbase_host, port=cfg.hbase_port,
        table_prefix=cfg.hbase_table_prefix or None,
    )
    person = conn.table("person")
    cursor_path = Path(cursor_file) if cursor_file else Path(cfg.file_root) / "geocode-upgrade-cursor.txt"
    prev = cursor_path.read_text().strip() if cursor_path.exists() else ""
    start = prev.encode("utf-8") if prev else None

    cands: dict[str, dict] = {}          # key -> address parts (one per candidate)
    refs: dict[str, tuple] = {}          # key -> (ukey, col, addr_dict)
    rows: dict[bytes, dict] = {}         # ukey -> {"ij": bytes|None, "cols": {col: att}}
    last: bytes | None = None
    wrapped = True  # stays True only if the scan reaches the table end this run

    lock = IngestLock(conn, owner="registry-faces:upgrade-geocode").acquire()
    try:
        for ukey, row in iter_scan(person, conn, columns=[b"a", b"i"], row_start=start,
                                   batch_size=200, max_restarts=10):
            last = ukey
            ij = row.get(b"i:json")
            for col, val in list(row.items()):
                c = col.decode()
                if not c.startswith("a:criminal:registry:"):
                    continue
                att = json.loads(val)
                for i, a in enumerate(att.get("addresses") or []):
                    if a.get("geo_precision") not in ("city", "state"):
                        continue  # already street/rooftop/exact — or unlocatable
                    if not (a.get("street") or "").strip():
                        continue  # no street to confirm
                    key = f"{ukey.decode()}|{c}|{i}"
                    cands[key] = {"street": a.get("street"), "city": a.get("city"),
                                  "state": a.get("state"), "zip": a.get("zip")}
                    refs[key] = (ukey, c, a)
                    rows.setdefault(ukey, {"ij": ij, "cols": {}})["cols"][c] = att
            if len(cands) >= limit:
                wrapped = False
                break

        hits = geo.geocode(cands) if cands else {}
        dirty: set[bytes] = set()
        upgraded = 0
        for key, r in hits.items():
            if r.source not in ("rooftop", "street"):
                continue  # a coarse Geocodio result is no better than our best-guess
            ukey, _c, a = refs[key]
            a["lat"], a["lon"], a["geo_precision"] = r.lat, r.lon, r.source
            dirty.add(ukey)
            upgraded += 1

        if not dry_run:
            for ukey in dirty:
                info = rows[ukey]
                updates = {col.encode("utf-8"): json.dumps(att).encode("utf-8")
                           for col, att in info["cols"].items()}
                if info["ij"]:
                    updates[b"i:json"] = info["ij"]  # bump ts for map delta-pull
                person.put(ukey, updates)
            # Advance the cursor (or reset to the top once we've swept the table).
            cursor_path.parent.mkdir(parents=True, exist_ok=True)
            cursor_path.write_text("" if wrapped else (last.decode("utf-8") if last else ""))
    finally:
        lock.release()
    click.echo(
        f"geocodio upgrade: candidates={len(cands)} upgraded={upgraded} "
        f"cursor={'wrapped-to-top' if wrapped else 'advanced'} (dry_run={dry_run})"
    )


@cli.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Show each adapter's last-run ingest health from indexes/health.json.

    Exits non-zero if any adapter is FAILING, so a cron/monitor can alert on a
    registry whose format changed without re-running the scrape.
    """
    from web_scrubber.health import load_health

    root = ctx.obj["registry_root"]
    data = load_health(Path(root) / "indexes" / "health.json")
    if not data:
        click.echo("(no health data yet — run `ingest <adapter>` first)")
        return
    failing = 0
    for name in sorted(data):
        last = data[name].get("last", {})
        status = last.get("status", "?")
        mark = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}.get(status, status)
        base = (data[name].get("baseline") or {}).get("records")
        extra = (f" (baseline {base})" if base is not None else "") + (
            f", {last['errors']} errors" if last.get("errors") else ""
        )
        click.echo(f"[{mark}] {name}: {last.get('records', '?')} records{extra}")
        for note in last.get("notes", []):
            click.echo(f"      - {note}")
        if status == "fail":
            failing += 1
    if failing:
        raise click.ClickException(f"{failing} adapter(s) FAILING — investigate (likely format changes)")


if __name__ == "__main__":
    cli()
