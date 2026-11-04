"""左侧竖向导航 (Fluent NavigationView 的简化版)

替掉原来顶部那五个标签页. 用 QListWidget 是为了白拿键盘上下切换与焦点框,
外观交给 theme.qss 的 QListWidget#Nav 规则.

图标走 Segoe Fluent Icons: 把字形渲染成 QIcon, 分别给 Normal / Selected
两种颜色, 选中时自动换色, 不用自己画.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from mdstudio.gui import theme

ITEM_H = 40
NAV_W = 172
ICON_PX = 18
NEUTRAL = "#8a8f98"     # 版本标签用中性灰, 深浅两套都看得清

# Segoe Fluent Icons 码位, 都是在机器上渲染出来看过再定的, 不是凭记忆
ICONS = {
    "cards": "\ue8a9",      # 四格
    "cutin": "\ue714",      # 摄影机
    "runfx": "\ue945",      # 闪电
    "pack": "\ue7b8",       # 盒子
    "hist": "\ue81c",       # 时钟箭头
    "settings": "\ue713",   # 齿轮
}


def glyph_icon(char: str, normal: str, selected: str, size: int = ICON_PX) -> QIcon:
    """把一个图标字形渲染成 QIcon, 带常态与选中态两种颜色"""
    icon = QIcon()
    for color, mode in ((normal, QIcon.Normal), (selected, QIcon.Selected)):
        px = QPixmap(size, size)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(theme.FONT_ICON)
        f.setPixelSize(size)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(px.rect(), Qt.AlignCenter, char)
        p.end()
        icon.addPixmap(px, mode, QIcon.Off)
    return icon


class Nav(QWidget):
    changed = Signal(int)

    def __init__(self, items: list[tuple[str, str]], footer: str = "", parent=None):
        """items 是 (键, 文案); 键用来取图标"""
        super().__init__(parent)
        self.setFixedWidth(NAV_W)
        self._keys = [k for k, _ in items]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 12, 8, 10)
        lay.setSpacing(8)

        self.list = QListWidget()
        self.list.setObjectName("Nav")
        self.list.setFrameShape(QListWidget.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setUniformItemSizes(True)
        self.list.setCursor(Qt.PointingHandCursor)
        self.list.setIconSize(QSize(ICON_PX, ICON_PX))
        for _, text in items:
            it = QListWidgetItem(text)
            it.setSizeHint(QSize(0, ITEM_H))
            self.list.addItem(it)
        lay.addWidget(self.list, 1)

        if footer:
            from mdstudio.gui.pill import Pill
            ver = Pill(footer, NEUTRAL)      # 透明底圆角标签
            ver.setObjectName("VersionChip")
            lay.addWidget(ver, 0, Qt.AlignHCenter)

        self.list.currentRowChanged.connect(self.changed)
        self.apply_theme(theme.tokens())

    def apply_theme(self, t: theme.Tokens) -> None:
        """重建图标: 常态用次要文字色, 选中用主文字色"""
        for i, key in enumerate(self._keys):
            ch = ICONS.get(key)
            if not ch:
                continue
            item = self.list.item(i)
            item.setIcon(glyph_icon(ch, t.muted, t.text))

    def current_row(self) -> int:
        return self.list.currentRow()

    def set_current(self, row: int) -> None:
        self.list.setCurrentRow(row)