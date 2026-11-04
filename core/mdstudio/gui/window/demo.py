"""材质与标题栏的独立演示窗口

    python -m mdstudio.gui.window.demo

P1 的验收工具: 只看窗口骨架, 不碰主界面. 主界面在 P2 接进来。
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from mdstudio.gui import theme
from mdstudio.gui.window import backdrop
from mdstudio.gui.window.titlebar import MicaWindow

ROWS = [
    ("青眼白龙", "有召唤动画", "2750 × 1.6 s"),
    ("黑魔导女孩", "有发动特效", "1920 × 1.2 s"),
    ("真红眼黑龙", "无", "—"),
    ("欧贝利斯克之巨神兵", "有召唤动画", "2750 × 1.8 s"),
]


class Demo(MicaWindow):
    def __init__(self):
        super().__init__("MD Cut-in Studio")
        self._vals: dict[str, QLabel] = {}

        body = self.body()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 14, 20, 20)
        lay.setSpacing(14)

        head = QLabel("窗口材质演示")
        head.setObjectName("H1")
        lay.addWidget(head)

        hint = QLabel("拖动标题栏移动；悬停最大化键弹出贴靠布局；右键标题栏出系统菜单；"
                      "拖到屏幕边缘自动贴靠。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        lay.addWidget(self._env_card())
        lay.addWidget(self._sample_card(), 1)

    # --- 环境信息 ---
    def _env_card(self) -> QWidget:
        card = QWidget()
        card.setObjectName("Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(26)
        grid.setVerticalSpacing(8)

        keys = ["材质档位", "系统主题", "强调色", "DPI 倍率", "窗口状态"]
        for i, k in enumerate(keys):
            name = QLabel(k)
            name.setObjectName("Muted")
            val = QLabel("—")
            self._vals[k] = val
            grid.addWidget(name, i, 0)
            grid.addWidget(val, i, 1)
        grid.setColumnStretch(1, 1)

        btn = QPushButton("重新探测")
        btn.clicked.connect(self._refresh_info)
        grid.addWidget(btn, 0, 2, 2, 1)

        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        return row

    # --- 控件样本 ---
    def _sample_card(self) -> QWidget:
        card = QWidget()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        title = QLabel("控件观感")
        title.setObjectName("H2")
        lay.addWidget(title)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        primary = QPushButton("主要动作")
        primary.setObjectName("Primary")
        btns.addWidget(primary)
        btns.addWidget(QPushButton("次要动作"))
        ghost = QPushButton("禁用状态")
        ghost.setEnabled(False)
        btns.addWidget(ghost)
        btns.addStretch(1)
        lay.addLayout(btns)

        tbl = QTableWidget(len(ROWS), 3)
        tbl.setHorizontalHeaderLabels(["卡片", "内容", "参数"])
        for r, row in enumerate(ROWS):
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                tbl.setItem(r, c, item)
        tbl.verticalHeader().setVisible(False)
        tbl.setShowGrid(False)
        tbl.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(tbl, 1)
        return card

    def _refresh_info(self) -> None:
        dpr = self.devicePixelRatioF()
        self._vals["材质档位"].setText(
            f"{backdrop.describe(self.material())}  ({self.material()})")
        self._vals["系统主题"].setText("深色" if theme.system_dark() else "浅色")
        self._vals["强调色"].setText(theme.accent())
        self._vals["DPI 倍率"].setText(f"{dpr:.2f}")
        self._vals["窗口状态"].setText("最大化" if self.isMaximized() else "普通")

    def showEvent(self, ev):
        super().showEvent(ev)
        self._refresh_info()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == ev.Type.WindowStateChange:
            self._refresh_info()


def main(argv=None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("MD Cut-in Studio")
    w = Demo()
    w.show()
    w.raise_()
    w.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
