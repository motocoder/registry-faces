"""First-run legal/ethics acknowledgment dialog.

Shows on the first launch (and any subsequent launch where the user hasn't
acknowledged the current version of the notes). User must check a box and
click OK before the app proceeds; cancel exits.

Acknowledgment is stored in the OS's standard app-config location via
`QStandardPaths`, keyed by a version string. Bumping `LEGAL_VERSION` here
will force a re-acknowledgment after a substantive change to LEGAL.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

# Bump when LEGAL.md gets a substantive update.
LEGAL_VERSION = "2026-05-21"

LEGAL_MD_PATH = Path(__file__).resolve().parents[3] / "LEGAL.md"

_SUMMARY_HTML = """
<p><b>This tool is for personal lookup of public sex offender registry data —
the same use case as visiting <a href="https://nsopw.gov">nsopw.gov</a> or
your state's official registry site, with normalized search across
jurisdictions.</b></p>

<p>Before you generate or run an adapter, you should understand:</p>

<ul>
  <li><b>You are responsible for the URL you point the agent at.</b> The
      agent will attempt to build an adapter for whatever you give it. It
      does not check the site's Terms of Service, robots.txt, or whether the
      site requires login / captcha / payment. Picking an appropriate URL
      is your job — point it only at sites you have the right to access in
      the way you're asking.</li>
  <li><b>Republishing is out of scope.</b> Don't host the data publicly or
      share it with people who couldn't get it themselves from the source.</li>
  <li><b>No facial recognition or identity-matching builds.</b> The agent
      will not aggregate identifying photos from non-registry sources — this
      is a hard rule. Only photo URLs from the source registry's own payload
      ever land in the store.</li>
  <li><b>State Terms of Service vary.</b> Some prohibit secondary databases
      or automated access at all. Read the state's ToS page before adding
      an adapter for it.</li>
  <li><b>Not for employment, housing, or credit decisions.</b> Those need
      an FCRA-compliant background-check provider, not this tool.</li>
  <li><b>Review generated code.</b> The agent writes Python files — open
      <code>adapters_generated/&lt;name&gt;.py</code> and skim it before
      running ingest.</li>
</ul>

<p>This is not legal advice. If you're unsure whether something is allowed
in your situation, consult an attorney in your jurisdiction.</p>
"""


def _settings_path() -> Path:
    """Return the OS-standard app-config file path."""
    loc = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if not loc:
        loc = str(Path.home() / ".registry-faces")
    return Path(loc) / "settings.local.json"


def _load_settings() -> dict:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def has_acknowledged_legal() -> bool:
    """True if the user has acknowledged the current LEGAL_VERSION."""
    return _load_settings().get("legal_acknowledged_version") == LEGAL_VERSION


def mark_acknowledged() -> None:
    data = _load_settings()
    data["legal_acknowledged_version"] = LEGAL_VERSION
    _save_settings(data)


class LegalDialog(QDialog):
    """First-run modal. Blocks the app until the user acknowledges or cancels."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("registry-faces — Read Before First Use")
        self.setMinimumWidth(640)
        self.setMinimumHeight(540)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Before you generate or run any adapter")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)

        body = QLabel(_SUMMARY_HTML)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        layout.addWidget(body, 1)

        # Link to full notes
        link_row = QHBoxLayout()
        link_btn = QPushButton("Open full notes (LEGAL.md)")
        link_btn.clicked.connect(self._open_full_notes)
        link_btn.setStyleSheet("padding: 6px 14px;")
        link_row.addWidget(link_btn)
        link_row.addStretch()
        layout.addLayout(link_row)

        # Acknowledgment checkbox
        self.checkbox = QCheckBox(
            "I have read LEGAL.md and understand the limitations and "
            "responsibilities of using this tool."
        )
        ck_font = QFont()
        ck_font.setPointSize(12)
        self.checkbox.setFont(ck_font)
        self.checkbox.toggled.connect(self._update_ok_enabled)
        layout.addWidget(self.checkbox)

        # OK / Cancel buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setEnabled(False)
        self._ok_btn.setText("I Agree — Continue")
        layout.addWidget(buttons)

    def _update_ok_enabled(self, checked: bool) -> None:
        self._ok_btn.setEnabled(checked)

    def _open_full_notes(self) -> None:
        if LEGAL_MD_PATH.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(LEGAL_MD_PATH)))


def ensure_legal_acknowledged() -> bool:
    """Show the dialog if needed. Returns True if the user proceeded, False if cancelled."""
    if has_acknowledged_legal():
        return True
    dialog = LegalDialog()
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    if accepted:
        mark_acknowledged()
    return accepted
