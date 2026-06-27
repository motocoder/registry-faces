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
def enrich_details(target: str | None, config_path: str, limit: int | None,
                   pause_seconds: float, kind: str, shard: str | None) -> None:
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
    with build_identity_service(cfg, lock=False) as bundle, BrowserFetcher() as fetcher:
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
def regeocode_hbase(config_path: str, dry_run: bool, force_unlock: bool) -> None:
    """Re-geocode existing REGISTRY attachments in HBase (disperse state/zip).

    Fixes data ingested before dispersing geocoding: tens of thousands of
    registry records were placed on the bare STATE CENTROID
    (geo_precision=state), so they stack on one point and the map can't break the
    cluster by zooming. This re-derives each registry address through the
    dispersing geocoder (state bbox / zip-city radius / random fallback), keeps
    genuine source coords (exact), and bumps i:json so the map delta-pull picks
    it up. Takes the global ingest lock; re-run if held.
    """
    import json
    from collections import Counter

    import happybase

    from web_scrubber.geocode import geocode_address, state_from_jurisdiction
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
    lock = IngestLock(conn, owner="registry-faces:regeocode-hbase").acquire()
    click.echo(f"lock acquired; scanning person table (dry_run={dry_run}) ...")
    prec: Counter = Counter()
    n_person = n_att = n_addr = 0
    try:
        for ukey, row in person.scan(columns=[b"a", b"i"], batch_size=500):
            changed = False
            for col, val in list(row.items()):
                c = col.decode()
                if not c.startswith("a:criminal:registry:"):
                    continue
                att = json.loads(val)
                src = att.get("source", {}) or {}
                jur, sid = src.get("jurisdiction", ""), src.get("source_id", "")
                fb = state_from_jurisdiction(jur)
                att_changed = False
                for i, a in enumerate(att.get("addresses") or []):
                    n_addr += 1
                    if a.get("geo_precision") == "exact" and a.get("lat") is not None:
                        prec["exact"] += 1
                        continue
                    res = geocode_address(
                        seed=f"{jur}:{sid}:{i}", state=a.get("state"),
                        city=a.get("city"), country=a.get("country"), fallback_state=fb,
                    )
                    a["lat"], a["lon"], a["geo_precision"] = res.lat, res.lon, res.source
                    prec[res.source] += 1
                    att_changed = True
                if att_changed:
                    n_att += 1
                    if not dry_run:
                        person.put(ukey, {col: json.dumps(att).encode("utf-8")})
                    changed = True
            if changed:
                n_person += 1
                ij = row.get(b"i:json")
                if ij and not dry_run:
                    person.put(ukey, {b"i:json": ij})  # bump ts for map delta-pull
                if n_person % 2000 == 0:
                    click.echo(f"  ... {n_person} persons")
    finally:
        lock.release()
    click.echo(f"persons updated={n_person} attachments={n_att} addresses={n_addr}")
    for p, c in prec.most_common():
        click.echo(f"  {p:10s} {c}")


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
