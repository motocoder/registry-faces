"""Detail panel for a single OffenderRecord, with photos.

Loads `record.json` and `photos/manifest.json` from the registry tree and
renders the fields. Photos are loaded from disk as QPixmaps and scaled to a
horizontal strip; clicking a thumbnail opens the file natively.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...photos import read_manifest
from ...schema import OffenderRecord


class _PhotoStrip(QWidget):
    THUMB_HEIGHT = 200

    def __init__(self, person_dir: Path) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        manifest = read_manifest(person_dir)
        if manifest is None or not manifest.photos:
            placeholder = QLabel("(no photos)")
            placeholder.setStyleSheet("color: #888;")
            layout.addWidget(placeholder)
            layout.addStretch()
            return

        photos_dir = person_dir / "photos"
        added = 0
        for entry in manifest.photos:
            if not entry.local_filename:
                continue
            path = photos_dir / entry.local_filename
            if not path.exists():
                continue
            pix = QPixmap(str(path))
            if pix.isNull():
                continue
            scaled = pix.scaledToHeight(self.THUMB_HEIGHT, Qt.TransformationMode.SmoothTransformation)
            label = QLabel()
            label.setPixmap(scaled)
            label.setFrameShape(QFrame.Shape.StyledPanel)
            label.setToolTip(entry.url)
            # Click to open natively
            label.mousePressEvent = lambda _e, p=path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))  # type: ignore[method-assign]
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(label)
            added += 1

        if added == 0:
            placeholder = QLabel("(photos not yet downloaded — run Sync Photos)")
            placeholder.setStyleSheet("color: #888;")
            layout.addWidget(placeholder)

        layout.addStretch()


class RecordView(QScrollArea):
    """The right-hand detail pane. Call `set_record()` to populate."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self._inner = QWidget()
        self._layout = QVBoxLayout(self._inner)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(16, 16, 16, 16)
        self._layout.setSpacing(10)
        self.setWidget(self._inner)
        self._show_placeholder()

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _show_placeholder(self) -> None:
        self._clear()
        label = QLabel("Select a record from the list to view details.")
        label.setStyleSheet("color: #888; font-style: italic;")
        self._layout.addWidget(label)

    def _h2(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 14px; font-weight: 600; margin-top: 8px;")
        return label

    def _kv(self, key: str, value: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        k = QLabel(key + ":")
        k.setStyleSheet("color: #555; min-width: 140px;")
        k.setFixedWidth(140)
        v = QLabel(value)
        v.setWordWrap(True)
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(k)
        layout.addWidget(v, 1)
        return row

    def set_record(self, record: OffenderRecord, person_dir: Path) -> None:
        self._clear()

        # Name as title
        title = QLabel(record.identity.full_name)
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._layout.addWidget(title)

        # Photo strip
        self._layout.addWidget(self._h2("Photos"))
        self._layout.addWidget(_PhotoStrip(person_dir))

        # Source
        self._layout.addWidget(self._h2("Source"))
        self._layout.addWidget(self._kv("Jurisdiction", record.source.jurisdiction))
        self._layout.addWidget(self._kv("Source ID", record.source.source_id))
        if record.source.first_seen_at:
            self._layout.addWidget(self._kv("First seen", record.source.first_seen_at.isoformat()))
        if record.source.fetched_at:
            self._layout.addWidget(self._kv("Last fetched", record.source.fetched_at.isoformat()))
        if record.source.info_url:
            btn = QPushButton("Open on source website")
            url = record.source.info_url
            btn.clicked.connect(lambda _checked=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            btn.setStyleSheet("padding: 6px 12px; margin-top: 4px;")
            self._layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # Identity
        self._layout.addWidget(self._h2("Identity"))
        ident = record.identity
        if ident.aliases:
            self._layout.addWidget(self._kv("Aliases", "; ".join(ident.aliases)))
        if ident.dob:
            self._layout.addWidget(self._kv("DOB", ident.dob.date().isoformat()))
        elif ident.year_of_birth:
            self._layout.addWidget(self._kv("Year of birth", str(ident.year_of_birth)))
        if ident.sex and ident.sex != "unknown":
            self._layout.addWidget(self._kv("Sex", ident.sex))
        if ident.race:
            self._layout.addWidget(self._kv("Race", ident.race))
        for k, v in [
            ("Height (cm)", ident.height_cm),
            ("Weight (kg)", ident.weight_kg),
            ("Eye color", ident.eye_color),
            ("Hair color", ident.hair_color),
            ("Description", ident.description),
        ]:
            if v:
                self._layout.addWidget(self._kv(k, str(v)))

        # Addresses
        if record.addresses:
            self._layout.addWidget(self._h2("Addresses"))
            for addr in record.addresses:
                parts = [addr.street, addr.city, addr.state, addr.zip]
                line = ", ".join(str(p) for p in parts if p)
                if addr.lat is not None and addr.lon is not None:
                    line += f"  ({addr.lat:.4f}, {addr.lon:.4f})"
                self._layout.addWidget(self._kv(addr.type, line))

        # Offenses
        if record.offenses:
            self._layout.addWidget(self._h2("Offenses"))
            for off in record.offenses:
                tier = f" [{off.tier_or_level_raw}]" if off.tier_or_level_raw else ""
                self._layout.addWidget(
                    self._kv(off.raw_code or "?", off.raw_description + tier)
                )

        # Registration
        reg = record.registration
        self._layout.addWidget(self._h2("Registration"))
        self._layout.addWidget(self._kv("Status", reg.status))
        if reg.absconder:
            self._layout.addWidget(self._kv("Absconder", "yes"))
        if reg.registered_since:
            self._layout.addWidget(self._kv("Registered since", reg.registered_since.date().isoformat()))
        if reg.next_verification:
            self._layout.addWidget(self._kv("Next verification", reg.next_verification.date().isoformat()))
