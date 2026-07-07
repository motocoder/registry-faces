"""Discover available adapters for the Scripts tab.

Two sources:
  * In-package adapters at `registry_faces.adapters.<name>` (excluding `base`).
  * User-generated adapters in the project-root `adapters_generated/*.py`.

Returns lightweight summaries — the actual Adapter instance is only built when
the user clicks "Ingest" or "Sync Photos" via the CLI's existing loader.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import adapters as _pkg_adapters

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GENERATED_DIR = PROJECT_ROOT / "adapters_generated"
EXCLUDED_PACKAGE_MODULES = {"base"}


@dataclass
class AdapterSummary:
    name: str
    kind: Literal["package", "generated"]
    source_path: Path
    jurisdiction: str
    source_name: str
    runnable: bool
    error: str | None = None

    def display_path(self) -> str:
        return str(self.source_path)


def _summarize_module(module_name: str, source_path: Path, kind: str) -> AdapterSummary:
    try:
        if kind == "package":
            module = importlib.import_module(module_name)
        else:
            spec = importlib.util.spec_from_file_location(module_name, source_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except Exception as e:
        return AdapterSummary(
            name=source_path.stem,
            kind=kind,  # type: ignore[arg-type]
            source_path=source_path,
            jurisdiction="?",
            source_name="?",
            runnable=False,
            error=f"{type(e).__name__}: {e}",
        )

    if not hasattr(module, "build"):
        return AdapterSummary(
            name=source_path.stem,
            kind=kind,  # type: ignore[arg-type]
            source_path=source_path,
            jurisdiction="?",
            source_name="?",
            runnable=False,
            error="module has no build() function",
        )

    # Pull metadata from the adapter class via build() without calling fetch().
    try:
        adapter = module.build()
        jurisdiction = getattr(adapter, "jurisdiction", "") or "?"
        source_name = getattr(adapter, "source_name", "") or "?"
        runnable = True
        error = None
    except Exception as e:
        jurisdiction = "?"
        source_name = "?"
        runnable = False
        error = f"build() raised: {type(e).__name__}: {e}"

    return AdapterSummary(
        name=source_path.stem,
        kind=kind,  # type: ignore[arg-type]
        source_path=source_path,
        jurisdiction=jurisdiction,
        source_name=source_name,
        runnable=runnable,
        error=error,
    )


def list_adapters() -> list[AdapterSummary]:
    summaries: list[AdapterSummary] = []
    pkg_path = Path(_pkg_adapters.__file__).parent

    # In-package adapters
    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        if module_info.name in EXCLUDED_PACKAGE_MODULES:
            continue
        module_name = f"registry_faces.adapters.{module_info.name}"
        source_path = pkg_path / f"{module_info.name}.py"
        summaries.append(_summarize_module(module_name, source_path, "package"))

    # Generated adapters
    if GENERATED_DIR.exists():
        for py_file in sorted(GENERATED_DIR.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module_name = f"adapters_generated.{py_file.stem}"
            summaries.append(_summarize_module(module_name, py_file, "generated"))

    return summaries
