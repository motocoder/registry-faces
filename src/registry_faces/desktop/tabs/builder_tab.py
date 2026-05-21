"""Builder tab — run the agent against a base URL to create/update an adapter."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...agent.providers import PRESETS, list_presets
from ..keys_dialog import KeysDialog
from ..widgets.log_view import LogView
from ..workers import BuildWorker


def _row_left(widget: QWidget) -> QWidget:
    """Wrap a compact widget so it sits left-aligned in a stretching form row."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)
    layout.addStretch()
    return container


def _row_with(*widgets: QWidget) -> QWidget:
    """Pack several widgets into a horizontal row with a trailing stretch."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        layout.addWidget(w)
    layout.addStretch()
    return container


class BuilderTab(QWidget):
    """Run the adapter-building agent."""

    adapter_built = Signal(str)  # emits adapter name when build finishes successfully

    def __init__(self, registry_root: Path, env_file: Path | None = None) -> None:
        super().__init__()
        self.registry_root = registry_root
        self.env_file = env_file
        self._worker: BuildWorker | None = None

        # ---- form ----
        expanding = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Text fields stretch to fill the form column.
        self.model = QLineEdit()
        self.model.setPlaceholderText("(default model for selected provider)")
        self.model.setSizePolicy(expanding)
        self.model.setMinimumWidth(400)

        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.gov  (base URL — agent will navigate)")
        self.url.setSizePolicy(expanding)
        self.url.setMinimumWidth(500)

        self.name = QLineEdit()
        self.name.setPlaceholderText("adapter name, e.g. florida (lowercase + underscores)")
        self.name.setSizePolicy(expanding)
        self.name.setMinimumWidth(300)

        self.jurisdiction = QLineEdit()
        self.jurisdiction.setPlaceholderText("e.g. US-FL")
        self.jurisdiction.setSizePolicy(expanding)
        self.jurisdiction.setMinimumWidth(200)

        # Combo boxes stay at their natural width (sized to longest item).
        self.provider = QComboBox()
        self.provider.addItems([""] + list_presets())  # blank = default
        self.provider.setToolTip(
            "LLM provider preset. Blank uses the default (REGISTRY_FACES_PROVIDER or 'anthropic')."
        )
        self.provider.currentTextChanged.connect(self._refresh_model_placeholder)
        self.provider.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

        self.keys_button = QPushButton("Keys…")
        self.keys_button.setToolTip("Edit API keys for the providers")
        self.keys_button.clicked.connect(self._on_open_keys)

        self.mode = QComboBox()
        self.mode.addItems(["auto", "create", "update"])
        self.mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

        self.run_button = QPushButton("Run agent")
        self.run_button.clicked.connect(self._on_run)
        self.run_button.setStyleSheet("padding: 8px 16px; font-weight: 600;")

        self.status_label = QLabel("idle")
        self.status_label.setStyleSheet("color: #888;")

        # ---- layout ----
        form = QFormLayout()
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addRow("Provider:", _row_with(self.provider, self.keys_button))
        form.addRow("Model override:", self.model)
        form.addRow("Base URL:", self.url)
        form.addRow("Adapter name:", self.name)
        form.addRow("Jurisdiction:", self.jurisdiction)
        form.addRow("Mode:", _row_left(self.mode))

        controls = QHBoxLayout()
        controls.addWidget(self.run_button)
        controls.addWidget(self.status_label, 1)

        self.log = LogView()

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addLayout(controls)
        outer.addWidget(QLabel("Agent output:"))
        outer.addWidget(self.log, 1)

    def _on_open_keys(self) -> None:
        dialog = KeysDialog(self.env_file, parent=self)
        dialog.exec()

    def _refresh_model_placeholder(self, preset: str) -> None:
        preset = (preset or "").strip()
        if preset in PRESETS:
            default = PRESETS[preset].get("model", "")
            self.model.setPlaceholderText(f"(default: {default})")
        else:
            self.model.setPlaceholderText("(default model for selected provider)")

    def _on_run(self) -> None:
        url = self.url.text().strip()
        name = self.name.text().strip()
        jurisdiction = self.jurisdiction.text().strip()
        if not url or not name or not jurisdiction:
            self.status_label.setText("URL, name, and jurisdiction are required.")
            self.status_label.setStyleSheet("color: #c33;")
            return

        provider = self.provider.currentText().strip() or None
        model = self.model.text().strip() or None
        mode = self.mode.currentText()

        self.run_button.setEnabled(False)
        self.status_label.setText("running...")
        self.status_label.setStyleSheet("color: #555;")
        self.log.clear_log()

        self._worker = BuildWorker(url, name, jurisdiction, provider, model, mode)
        self._worker.log_line.connect(self.log.append_line)
        self._worker.finished_with_report.connect(self._on_finished)
        self._worker.start()

    def _on_finished(self, success: bool, report: str) -> None:
        self.run_button.setEnabled(True)
        if success:
            self.status_label.setText("done.")
            self.status_label.setStyleSheet("color: #393;")
            self.log.append_line("")
            self.log.append_line("=== REPORT ===")
            self.log.append_line(report)
            self.adapter_built.emit(self.name.text().strip())
        else:
            self.status_label.setText("failed.")
            self.status_label.setStyleSheet("color: #c33;")
            self.log.append_line("")
            self.log.append_line("=== ERROR ===")
            self.log.append_line(report)
        self._worker = None
