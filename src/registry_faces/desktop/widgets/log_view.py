"""A read-only log panel that auto-scrolls and survives a few thousand lines."""

from __future__ import annotations

from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit


class LogView(QPlainTextEdit):
    def __init__(self, max_lines: int = 5000) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_lines)
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(11)
        self.setFont(font)
        self.setPlaceholderText("(no output yet)")

    def append_line(self, line: str) -> None:
        self.appendPlainText(line)
        self.moveCursor(QTextCursor.MoveOperation.End)

    def clear_log(self) -> None:
        self.clear()
