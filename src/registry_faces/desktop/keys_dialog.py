"""Dialog for entering / updating provider API keys, persisted to `.env`.

Lists every credential env var referenced by the provider presets (skipping
Ollama, which has none). On open, fields are pre-populated from the current
`.env` file (and the live process env as a fallback). On Save, values are
written back to `.env` via `dotenv.set_key`, which preserves the rest of
the file. Empty fields are removed from `.env` rather than written as blank.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from ..agent.providers import PRESETS


def _credential_env_vars() -> list[tuple[str, str]]:
    """Return [(preset_name, env_var_name), ...] for presets that take a key, in display order."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for preset_name, cfg in PRESETS.items():
        env_var = cfg.get("api_key_env")
        if not env_var or env_var in seen:
            continue
        seen.add(env_var)
        out.append((preset_name, env_var))
    return out


class KeysDialog(QDialog):
    """Edit provider API keys; saves to the configured `.env` file."""

    def __init__(self, env_file: Path | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("API Keys")
        self.setMinimumWidth(560)
        self.setModal(True)

        # Resolve where we'll read/write. If env_file wasn't supplied, default
        # to ./.env in the project root.
        if env_file is None:
            env_file = Path.cwd() / ".env"
        self.env_file = env_file.resolve()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Provider API Keys")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        layout.addWidget(title)

        hint = QLabel(
            "All keys are <b>optional</b> — only the ones for the providers "
            "you actually use are required. Empty fields are removed from "
            f"<code>.env</code> on save.<br><br>Writing to: "
            f"<code>{self.env_file}</code>"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        layout.addWidget(hint)

        # Load existing values
        existing = dict(dotenv_values(self.env_file)) if self.env_file.exists() else {}

        # Build a row for each credential env var
        form = QFormLayout()
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)

        self._fields: dict[str, QLineEdit] = {}
        for preset_name, env_var in _credential_env_vars():
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setFont(mono)
            edit.setMinimumWidth(380)
            # Pre-populate: prefer the .env file value, fall back to the live process env
            value = existing.get(env_var) or os.environ.get(env_var, "")
            edit.setText(value)
            edit.setPlaceholderText(f"(unset — required for --provider {preset_name})")
            form.addRow(f"{env_var}:", edit)
            self._fields[env_var] = edit

        layout.addLayout(form)

        # Save / Cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self) -> None:
        try:
            # Make sure the file exists; dotenv.set_key needs that.
            self.env_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.env_file.exists():
                self.env_file.touch()
            existing_keys = set(dotenv_values(self.env_file).keys())
            for env_var, edit in self._fields.items():
                value = edit.text().strip()
                if value:
                    set_key(str(self.env_file), env_var, value, quote_mode="never")
                    os.environ[env_var] = value
                elif env_var in existing_keys:
                    # Was previously set; user cleared the field -> remove from .env
                    unset_key(str(self.env_file), env_var)
                    os.environ.pop(env_var, None)
                # else: empty field for a key that wasn't in .env — nothing to do
        except Exception as e:
            QMessageBox.critical(
                self,
                "Couldn't save keys",
                f"{type(e).__name__}: {e}\n\nFile: {self.env_file}",
            )
            return
        self.accept()
