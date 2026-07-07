"""Project-level smoke tests for path and discovery wiring."""
from __future__ import annotations

from pathlib import Path

from registry_faces import cli
from registry_faces.desktop import adapter_discovery


def test_generated_adapter_paths_are_project_rooted():
    project_root = Path(__file__).resolve().parents[1]

    assert cli.ADAPTERS_OUT == project_root / "adapters_generated"
    assert adapter_discovery.GENERATED_DIR == project_root / "adapters_generated"


def test_desktop_adapter_discovery_loads_package_adapters():
    adapters = adapter_discovery.list_adapters()

    assert adapters
    assert any(a.kind == "package" for a in adapters)
    assert all(a.source_path.is_absolute() for a in adapters)
