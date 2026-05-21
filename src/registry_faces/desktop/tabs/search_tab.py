"""Search tab — query the store and view records with photos."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...schema import OffenderRecord
from ...store import FileStore
from ..widgets.record_view import RecordView


class _ResultRow(QWidget):
    """A row in the results list: name + location text and (if available) an
    Open button that takes the user straight to the source registry's page."""

    def __init__(self, record: OffenderRecord) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        location = ""
        if record.addresses:
            a = record.addresses[0]
            city = a.city or "?"
            state = a.state or "?"
            location = f"  ({city}, {state})"
        label = QLabel(f"[{record.source.jurisdiction}] {record.identity.full_name}{location}")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(label, 1)

        if record.source.info_url:
            url = record.source.info_url
            btn = QPushButton("Open ↗")
            btn.setToolTip(f"Open {url}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFlat(False)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # don't steal selection focus
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(lambda _checked=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            layout.addWidget(btn)


class SearchTab(QWidget):
    def __init__(self, registry_root: Path) -> None:
        super().__init__()
        self.registry_root = registry_root

        # ---- search controls ----
        self.mode = QComboBox()
        self.mode.addItems(["Name", "ZIP", "Geo radius"])
        self.mode.currentIndexChanged.connect(self._on_mode_changed)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. SMITH")
        self.name_input.returnPressed.connect(self._on_search)

        self.zip_input = QLineEdit()
        self.zip_input.setPlaceholderText("e.g. 57104")
        self.zip_input.returnPressed.connect(self._on_search)

        self.lat_input = QLineEdit()
        self.lat_input.setPlaceholderText("latitude (e.g. 43.5444)")
        self.lon_input = QLineEdit()
        self.lon_input.setPlaceholderText("longitude (e.g. -96.7341)")
        self.radius_input = QLineEdit()
        self.radius_input.setPlaceholderText("radius in meters (default 1609)")

        geo_widget = QWidget()
        geo_layout = QHBoxLayout(geo_widget)
        geo_layout.setContentsMargins(0, 0, 0, 0)
        geo_layout.addWidget(self.lat_input)
        geo_layout.addWidget(self.lon_input)
        geo_layout.addWidget(self.radius_input)

        self.query_stack = QStackedWidget()
        self.query_stack.addWidget(self.name_input)
        self.query_stack.addWidget(self.zip_input)
        self.query_stack.addWidget(geo_widget)

        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._on_search)
        self.search_btn.setStyleSheet("padding: 6px 14px; font-weight: 600;")

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888;")

        controls = QHBoxLayout()
        controls.addWidget(self.mode)
        controls.addWidget(self.query_stack, 1)
        controls.addWidget(self.search_btn)

        # ---- results + detail ----
        self.results_list = QListWidget()
        self.results_list.setMinimumWidth(280)
        self.results_list.itemSelectionChanged.connect(self._on_result_selected)

        self.detail = RecordView()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.results_list)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 700])

        outer = QVBoxLayout(self)
        outer.addLayout(controls)
        outer.addWidget(self.status_label)
        outer.addWidget(splitter, 1)

        self._last_results: list[OffenderRecord] = []

    def _on_mode_changed(self, idx: int) -> None:
        self.query_stack.setCurrentIndex(idx)

    def _on_search(self) -> None:
        mode = self.mode.currentText()
        self.results_list.clear()
        self._last_results = []
        try:
            with FileStore(self.registry_root) as store:
                if mode == "Name":
                    q = self.name_input.text().strip()
                    if not q:
                        self.status_label.setText("Enter a name fragment.")
                        return
                    results = store.search_name(q, limit=200)
                elif mode == "ZIP":
                    q = self.zip_input.text().strip()
                    if not q:
                        self.status_label.setText("Enter a ZIP code.")
                        return
                    results = store.search_zip(q)
                else:  # Geo
                    try:
                        lat = float(self.lat_input.text().strip())
                        lon = float(self.lon_input.text().strip())
                    except ValueError:
                        self.status_label.setText("Lat and Lon must be numbers.")
                        return
                    radius_str = self.radius_input.text().strip() or "1609"
                    try:
                        radius = float(radius_str)
                    except ValueError:
                        self.status_label.setText("Radius must be a number (meters).")
                        return
                    results = store.search_radius(lat, lon, radius)
        except Exception as e:
            self.status_label.setText(f"Search failed: {type(e).__name__}: {e}")
            self.status_label.setStyleSheet("color: #c33;")
            return

        self._last_results = results
        for r in results:
            item = QListWidgetItem()
            row = _ResultRow(r)
            item.setSizeHint(row.sizeHint())
            self.results_list.addItem(item)
            self.results_list.setItemWidget(item, row)

        self.status_label.setText(f"{len(results)} match(es).")
        self.status_label.setStyleSheet("color: #888;")

    def _on_result_selected(self) -> None:
        idx = self.results_list.currentRow()
        if idx < 0 or idx >= len(self._last_results):
            return
        record = self._last_results[idx]
        person_dir = (
            self.registry_root
            / "records"
            / record.source.jurisdiction
            / record.source.source_id
        )
        self.detail.set_record(record, person_dir)
