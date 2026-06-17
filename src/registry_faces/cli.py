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

import importlib.util
import os
from pathlib import Path

import click
from dotenv import load_dotenv

from .adapters.base import Adapter
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


def _load_adapter(name: str) -> Adapter:
    # First try in-package adapters (registry_faces.adapters.<name>)
    pkg_module_name = f"registry_faces.adapters.{name}"
    try:
        import importlib

        module = importlib.import_module(pkg_module_name)
    except ImportError:
        module = None

    if module is None:
        # Fall back to user-generated adapters in ./adapters_generated/
        path = ADAPTERS_OUT / f"{name}.py"
        if not path.exists():
            raise click.ClickException(
                f"No adapter named {name!r}. Looked in registry_faces.adapters "
                f"and {path}. Run `registry-faces build <url> --name {name}` first."
            )
        spec = importlib.util.spec_from_file_location(f"adapters_generated.{name}", path)
        if spec is None or spec.loader is None:
            raise click.ClickException(f"Could not load {path}.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    if not hasattr(module, "build"):
        raise click.ClickException(f"Adapter {name!r} has no build() function.")
    return module.build()


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


@cli.command("ingest-identity")
@click.argument("name")
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
def ingest_identity(name: str, target: str | None, config_path: str) -> None:
    """Run an adapter and feed its records into the centralized person identity.

    Dry-run to local files by default; pass --to hbase (or set identity.mode=hbase
    in the properties file) to write to HBase once it's set up.
    """
    from web_scrubber.person.config import build_identity_service, load_config
    from web_scrubber.person.ingest import ingest_adapter

    from .identity_map import map_item

    adapter = _load_adapter(name)
    cfg = load_config(config_path, mode_override=target)
    click.echo(f"Identity ingest: {name} ({adapter.jurisdiction}) -> {cfg.mode}")
    with build_identity_service(cfg) as bundle:
        stats = ingest_adapter(
            bundle.service, adapter, map_item, on_progress=lambda n: click.echo(f"  ... {n}")
        )
    click.echo(stats.line())


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
    with build_identity_service(cfg) as bundle:
        stats = bundle.sync_photos(refresh=refresh, user_agent=photo_ua, pause_seconds=pause_seconds)
    click.echo(stats.line())
    for url, err in stats.failed[:10]:
        click.echo(f"  FAIL {url} -- {err}")


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
