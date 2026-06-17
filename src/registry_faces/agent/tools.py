"""Agent tools — registry-faces binding over ``web_scrubber.agent.tools``.

The tool implementations live in the framework; here we only build the
``AgentContext`` pointing them at registry-faces' schema, reference adapter
(Hawaii), record class, and output dir. ``CTX`` is exported for the builder.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from web_scrubber.agent.tools import AgentContext, make_tools as _make_tools

from ..schema import OffenderRecord

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ADAPTERS_OUT = PROJECT_ROOT / "adapters_generated"
SCHEMA_PATH = PROJECT_ROOT / "src" / "registry_faces" / "schema.py"
REFERENCE_ADAPTER_PATH = PROJECT_ROOT / "src" / "registry_faces" / "adapters" / "hawaii.py"
BASE_ADAPTER_PATH = PROJECT_ROOT / "src" / "registry_faces" / "adapters" / "base.py"


def _summarize(rec: OffenderRecord) -> str:
    addr = ""
    if rec.addresses:
        a = rec.addresses[0]
        addr = f"  [{a.city or '?'}, {a.state or '?'}]"
    return f"{rec.identity.full_name} ({rec.source.source_id}){addr}"


CTX = AgentContext(
    record_cls=OffenderRecord,
    schema_path=SCHEMA_PATH,
    reference_adapter_path=REFERENCE_ADAPTER_PATH,
    base_adapter_path=BASE_ADAPTER_PATH,
    adapters_out=ADAPTERS_OUT,
    record_label="record",
    summarize=_summarize,
)


def make_tools() -> list[Callable]:
    """Return the agent tool list bound to the registry-faces context."""
    return _make_tools(CTX)
