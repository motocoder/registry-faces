"""Tools the adapter-building agent can call.

These are plain Python functions — no provider-specific decorators. Each
provider in `providers/` converts this list to its native tool format and
dispatches calls. The functions themselves never know which LLM is calling
them.

Docstring convention: first paragraph = description, `Args:` block = parameter
descriptions. The introspection in `tool_schema.py` reads these.
"""

import importlib.util
import json
import re
from pathlib import Path

import httpx

from ..schema import OffenderRecord

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ADAPTERS_OUT = PROJECT_ROOT / "adapters_generated"
SCHEMA_PATH = PROJECT_ROOT / "src" / "registry_faces" / "schema.py"
REFERENCE_ADAPTER_PATH = PROJECT_ROOT / "src" / "registry_faces" / "adapters" / "hawaii.py"
BASE_ADAPTER_PATH = PROJECT_ROOT / "src" / "registry_faces" / "adapters" / "base.py"


def head_url(url: str) -> str:
    """Get URL metadata as a JSON string (status, content-type, content-length, final URL after redirects).

    Use this first to classify what kind of resource the URL serves. Parse
    the returned JSON to inspect the fields.

    Args:
        url: The URL to inspect.
    """
    with httpx.Client(
        follow_redirects=True, timeout=30, headers={"User-Agent": _DEFAULT_UA}
    ) as c:
        resp = c.head(url)
        if resp.status_code in (403, 405, 501):
            resp = c.get(url, headers={"Range": "bytes=0-1023"})
    return json.dumps(
        {
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "content_length": resp.headers.get("content-length"),
            "final_url": str(resp.url),
        }
    )


def fetch_url(url: str, max_bytes: int = 40000) -> str:
    """Fetch a URL and return its content as text, capped at max_bytes.

    Use this to inspect HTML structure, sample JSON, sample CSV, etc. The cap
    keeps large bulk files from blowing the context window — for investigation
    a sample is enough.

    Args:
        url: The URL to fetch.
        max_bytes: Truncate response body to this many bytes (default 40000).
    """
    with httpx.Client(
        follow_redirects=True, timeout=60, headers={"User-Agent": _DEFAULT_UA}
    ) as c:
        resp = c.get(url)
        resp.raise_for_status()
    body = resp.text
    if len(body) > max_bytes:
        return body[:max_bytes] + f"\n\n... [truncated, {len(body) - max_bytes} more bytes]"
    return body


def read_schema() -> str:
    """Return the canonical OffenderRecord schema source. Read this before writing an adapter."""
    return SCHEMA_PATH.read_text()


def read_reference_adapter() -> str:
    """Return the Hawaii reference adapter source. New adapters should follow this shape."""
    return REFERENCE_ADAPTER_PATH.read_text()


def read_base_adapter() -> str:
    """Return the Adapter base class source."""
    return BASE_ADAPTER_PATH.read_text()


def read_existing_adapter(name: str) -> str:
    """Return the source of an existing adapter at adapters_generated/<name>.py.

    Use this in update mode to see what's already there before deciding what
    (if anything) needs to change. Returns "(no existing adapter)" if the
    file doesn't exist — distinguish missing from empty.

    Args:
        name: Adapter name (no .py extension).
    """
    path = ADAPTERS_OUT / f"{name}.py"
    if not path.exists():
        return "(no existing adapter)"
    return path.read_text()


def write_adapter(name: str, python_code: str) -> str:
    """Write an adapter module to adapters_generated/<name>.py.

    The module must subclass `Adapter`, implement `fetch()` + `normalize()`,
    and expose a module-level `build()` returning an instance.

    Args:
        name: Adapter name. Lowercase letters, digits, underscores. e.g. "florida".
        python_code: Full module source code.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return f"ERROR: invalid name {name!r}. Use lowercase + digits + underscores only."
    ADAPTERS_OUT.mkdir(exist_ok=True)
    target = ADAPTERS_OUT / f"{name}.py"
    target.write_text(python_code)
    return f"Wrote {target} ({len(python_code)} bytes)."


def test_adapter(name: str, max_records: int = 5) -> str:
    """Load an adapter from adapters_generated/<name>.py, run it, validate records.

    Imports the module, calls `build()`, iterates `adapter.run()` up to
    max_records. Each record is validated as OffenderRecord. Returns a report.

    Args:
        name: Adapter name (no .py extension).
        max_records: Stop after this many records.
    """
    path = ADAPTERS_OUT / f"{name}.py"
    if not path.exists():
        return f"ERROR: {path} does not exist. Call write_adapter first."

    spec = importlib.util.spec_from_file_location(f"adapters_generated.{name}", path)
    if spec is None or spec.loader is None:
        return "ERROR: could not load module spec."
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        return f"ERROR loading module: {type(e).__name__}: {e}"

    if not hasattr(module, "build"):
        return "ERROR: module must define a `build()` function returning an Adapter."

    try:
        adapter = module.build()
    except Exception as e:
        return f"ERROR calling build(): {type(e).__name__}: {e}"

    lines: list[str] = []
    errors: list[str] = []
    count = 0
    try:
        for record in adapter.run():
            count += 1
            if isinstance(record, OffenderRecord):
                addr = ""
                if record.addresses:
                    a = record.addresses[0]
                    addr = f"  [{a.city or '?'}, {a.state or '?'}]"
                lines.append(
                    f"  [{count}] {record.identity.full_name} ({record.source.source_id}){addr}"
                )
            else:
                errors.append(
                    f"  [{count}] expected OffenderRecord, got {type(record).__name__}"
                )
            if count >= max_records:
                break
    except Exception as e:
        return (
            f"ERROR during adapter.run(): {type(e).__name__}: {e}\n"
            f"Pulled {count} records before failure."
        )

    if count == 0:
        return "ERROR: adapter yielded zero records. Check fetch() and pagination."

    header = f"OK. Pulled {count} records."
    if errors:
        header = f"VALIDATION ISSUES ({len(errors)} of {count} records):"
        return header + "\n" + "\n".join(errors) + "\nValid records:\n" + "\n".join(lines)
    return header + "\n" + "\n".join(lines)


TOOLS = [
    head_url,
    fetch_url,
    read_schema,
    read_reference_adapter,
    read_base_adapter,
    read_existing_adapter,
    write_adapter,
    test_adapter,
]
