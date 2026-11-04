"""分段切换控件 (WinUI 的 SelectorBar 简化版)

用来在互斥的几件事之间切换. 这里的场景是"视频 / GIF / 帧文件夹"与"Spine"
两种导入方式: 它们本来就互斥, 并排堆两张卡片会让人分不清按钮属于谁,
换成一次只显示一种就没有归属问题了.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QWidget


class Segmented(QWidget):
    changed = Signal(int)

    def __init__(self, labels: list[str], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setObjectName("Seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self._group.addButton(b, i)
            lay.addWidget(b)
            self._buttons.append(b)
        lay.addStretch(1)
        if self._buttons:
            self._buttons[0].setChecked(True)
        self._group.idClicked.connect(self.changed)

    def set_index(self, i: int) -> None:
        if 0 <= i < len(self._buttons):
            self._buttons[i].setChecked(True)
            self.changed.emit(i)

    def index(self) -> int:
        return self._group.checkedId()