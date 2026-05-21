"""Main window + entry point for the desktop UI."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QTabWidget,
)

# Look for the app icon next to the package (../../assets/icon.png from this file)
_ICON_PATH = Path(__file__).resolve().parents[3] / "assets" / "icon.png"

from .legal_dialog import ensure_legal_acknowledged
from .tabs.builder_tab import BuilderTab
from .tabs.scripts_tab import ScriptsTab
from .tabs.search_tab import SearchTab


class MainWindow(QMainWindow):
    def __init__(self, registry_root: Path, env_file: Path | None = None) -> None:
        super().__init__()
        self.registry_root = registry_root.resolve()
        self.env_file = env_file
        self.setWindowTitle(f"registry-faces — {self.registry_root}")
        self.resize(1200, 800)
        if _ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(_ICON_PATH)))

        self.tabs = QTabWidget()
        self.search_tab = SearchTab(self.registry_root)
        self.scripts_tab = ScriptsTab(self.registry_root)
        self.builder_tab = BuilderTab(self.registry_root, env_file=self.env_file)

        self.tabs.addTab(self.search_tab, "Search")
        self.tabs.addTab(self.scripts_tab, "Scripts")
        self.tabs.addTab(self.builder_tab, "Builder")
        self.setCentralWidget(self.tabs)

        # Wire cross-tab signals
        self.builder_tab.adapter_built.connect(lambda _name: self.scripts_tab.refresh())
        self.scripts_tab.data_changed.connect(self._on_data_changed)

        self._build_menu()

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")

        open_action = QAction("&Open Registry Folder...", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_registry)
        file_menu.addAction(open_action)

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _on_open_registry(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Choose registry root",
            str(self.registry_root),
            QFileDialog.Option.ShowDirsOnly,
        )
        if chosen:
            # Simplest approach: replace tabs with new ones bound to the new root.
            new_root = Path(chosen).resolve()
            self.registry_root = new_root
            self.setWindowTitle(f"registry-faces — {self.registry_root}")
            self.tabs.clear()
            self.search_tab = SearchTab(self.registry_root)
            self.scripts_tab = ScriptsTab(self.registry_root)
            self.builder_tab = BuilderTab(self.registry_root, env_file=self.env_file)
            self.tabs.addTab(self.search_tab, "Search")
            self.tabs.addTab(self.scripts_tab, "Scripts")
            self.tabs.addTab(self.builder_tab, "Builder")
            self.builder_tab.adapter_built.connect(lambda _name: self.scripts_tab.refresh())
            self.scripts_tab.data_changed.connect(self._on_data_changed)

    def _on_data_changed(self) -> None:
        # If the user is on the search tab and data just changed, surface that.
        if self.tabs.currentWidget() is self.search_tab:
            self.search_tab.status_label.setText(
                "Store updated. Re-run your search to see fresh results."
            )


def main(registry_root: Path | None = None, env_file: Path | None = None) -> None:
    if registry_root is None:
        registry_root = Path("registry")
    registry_root.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("registry-faces")
    app.setApplicationDisplayName("registry-faces")
    if _ICON_PATH.exists():
        # On macOS this also sets the dock icon when launched from a script.
        app.setWindowIcon(QIcon(str(_ICON_PATH)))

    # Block until the user acknowledges LEGAL.md (or cancels and exits).
    if not ensure_legal_acknowledged():
        sys.exit(0)

    win = MainWindow(registry_root, env_file=env_file)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
