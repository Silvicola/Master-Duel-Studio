"""可收起的「高级」区块

用得少、门槛高、又不想删的功能放这里: 默认收起, 想用的人展开.
比直接删掉稳妥, 也比让它常驻占地方强.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget


class Collapsible(QWidget):
    def __init__(self, title: str, content: QWidget, parent=None):
        super().__init__(parent)
        self._title = title
        self._content = content
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        self.btn = QPushButton()
        self.btn.setObjectName("Expander")
        self.btn.setCheckable(True)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.toggled.connect(self._toggle)
        v.addWidget(self.btn, 0, Qt.AlignLeft)
        v.addWidget(content)
        self._set_label(False)
        content.hide()

    def _set_label(self, on: bool) -> None:
        self.btn.setText(("▾  " if on else "▸  ") + self._title)

    def _toggle(self, on: bool) -> None:
        self._set_label(on)
        self._content.setVisible(on)