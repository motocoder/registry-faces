"""Scripts tab — list adapters and run Ingest / Sync Photos / Verify on them."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..adapter_discovery import AdapterSummary, list_adapters
from ..widgets.log_view import LogView
from ..workers import IngestWorker, SyncPhotosWorker, VerifyWorker


COLUMNS = ["Name", "Kind", "Jurisdiction", "Source", "Runnable"]


class ScriptsTab(QWidget):
    """List adapters and run them."""

    data_changed = Signal()  # emitted after ingest/sync so search tab refreshes

    def __init__(self, registry_root: Path) -> None:
        super().__init__()
        self.registry_root = registry_root
        self._worker = None

        # ---- table ----
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        # ---- action buttons ----
        self.ingest_btn = QPushButton("Ingest")
        self.ingest_btn.clicked.connect(self._on_ingest)
        self.sync_btn = QPushButton("Sync Photos")
        self.sync_btn.clicked.connect(self._on_sync_photos)
        self.verify_btn = QPushButton("Verify")
        self.verify_btn.clicked.connect(self._on_verify)
        self.open_btn = QPushButton("Open Source")
        self.open_btn.clicked.connect(self._on_open_source)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setStyleSheet("color: #b22;")
        self.delete_btn.clicked.connect(self._on_delete)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        action_row = QHBoxLayout()
        for b in (self.ingest_btn, self.sync_btn, self.verify_btn, self.open_btn, self.delete_btn):
            action_row.addWidget(b)
        action_row.addStretch()
        action_row.addWidget(self.refresh_btn)

        self.status_label = QLabel("idle")
        self.status_label.setStyleSheet("color: #888;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m")
        self.progress_bar.setVisible(False)

        self.log = LogView()

        outer = QVBoxLayout(self)
        outer.addWidget(self.table, 2)
        outer.addLayout(action_row)
        outer.addWidget(self.status_label)
        outer.addWidget(self.progress_bar)
        outer.addWidget(self.log, 1)

        self.refresh()
        self._update_action_buttons()

    # ---- table population ----

    def refresh(self) -> None:
        adapters = list_adapters()
        self.table.setRowCount(len(adapters))
        for row, a in enumerate(adapters):
            for col, value in enumerate(self._row_cells(a)):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, a.name)
                if not a.runnable and col == 4:
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(row, col, item)
        self._adapters_by_row = adapters
        self._update_action_buttons()

    def _row_cells(self, a: AdapterSummary) -> list[str]:
        return [
            a.name,
            a.kind,
            a.jurisdiction,
            a.display_path(),
            "yes" if a.runnable else f"no ({a.error})",
        ]

    def _selected_adapter(self) -> AdapterSummary | None:
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        if not rows:
            return None
        row = next(iter(rows))
        if row >= len(self._adapters_by_row):
            return None
        return self._adapters_by_row[row]

    def _on_selection_changed(self) -> None:
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        sel = self._selected_adapter()
        has_sel = sel is not None
        busy = self._worker is not None
        for b in (self.ingest_btn, self.sync_btn, self.verify_btn, self.open_btn):
            b.setEnabled(has_sel and not busy)
        # Delete: only for generated, and only when not busy
        is_generated = bool(has_sel and sel and sel.kind == "generated")
        self.delete_btn.setEnabled(is_generated and not busy)
        # Ingest: only if runnable
        if has_sel and sel:
            self.ingest_btn.setEnabled(sel.runnable and not busy)

    # ---- actions ----

    def _start_worker(self, worker, label: str) -> None:
        self._worker = worker
        self.status_label.setText(f"{label} running...")
        self.status_label.setStyleSheet("color: #555;")
        self.log.clear_log()
        # Indeterminate state until first progress signal lands
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        worker.log_line.connect(self.log.append_line)
        worker.progress.connect(self._on_progress)
        worker.finished_with_report.connect(lambda ok, msg, l=label: self._on_worker_done(ok, msg, l))
        self._update_action_buttons()
        worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            # Determinate: known total (sync-photos pre-counts; ingest emits total==done at the end)
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        else:
            # Indeterminate: show the running count via the format string
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(f"{done} processed")

    def _on_worker_done(self, success: bool, message: str, label: str) -> None:
        self._worker = None
        self.log.append_line("")
        self.log.append_line(f"=== {label} {'OK' if success else 'FAILED'} ===")
        self.log.append_line(message)
        self.status_label.setText(f"{label} {'done' if success else 'failed'}.")
        self.status_label.setStyleSheet("color: #393;" if success else "color: #c33;")
        self.progress_bar.setVisible(False)
        self.progress_bar.setFormat("%v / %m")
        self._update_action_buttons()
        if success:
            self.data_changed.emit()

    def _on_ingest(self) -> None:
        sel = self._selected_adapter()
        if sel is None:
            return
        self._start_worker(IngestWorker(sel.name, self.registry_root), f"Ingest {sel.name}")

    def _on_sync_photos(self) -> None:
        sel = self._selected_adapter()
        if sel is None:
            return
        jur = sel.jurisdiction if sel.jurisdiction != "?" else None
        self._start_worker(
            SyncPhotosWorker(jur, self.registry_root),
            f"Sync photos for {sel.name} ({jur or 'all'})",
        )

    def _on_verify(self) -> None:
        self._start_worker(VerifyWorker(self.registry_root), "Verify")

    def _on_open_source(self) -> None:
        sel = self._selected_adapter()
        if sel is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(sel.source_path)))

    def _on_delete(self) -> None:
        sel = self._selected_adapter()
        if sel is None or sel.kind != "generated":
            return
        ans = QMessageBox.question(
            self,
            "Delete adapter",
            f"Delete {sel.source_path}?\n\nThis removes the generated adapter file. "
            "Records already in the store are NOT affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            sel.source_path.unlink()
            self.status_label.setText(f"Deleted {sel.source_path.name}.")
            self.status_label.setStyleSheet("color: #393;")
            self.refresh()
        except Exception as e:
            self.status_label.setText(f"Delete failed: {type(e).__name__}: {e}")
            self.status_label.setStyleSheet("color: #c33;")
