"""状态标签: 圆角矩形、透明底、彩色描边与文字

两种用法:
    表格    挂 PillDelegate (只画不建控件, 300 行也不卡)
    列表    setItemWidget 放一个 PillRow (列表项要按状态显示不同文字)

为什么透明底: 表格与列表本身有选中底色, 标签再叠一层填充会把底色盖掉,
看着像贴了张便利贴. 只留描边和文字, 颜色区分状态就够.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem, QWidget)

ROLE_COLOR = Qt.UserRole + 11      # 单元格里存状态颜色
PAD_X = 9
PAD_Y = 3
RADIUS = 4
LEFT = 7                           # 标签离单元格左边多远


def _edge(color: QColor) -> QColor:
    c = QColor(color)
    c.setAlpha(110)
    return c


class PillDelegate(QStyledItemDelegate):
    """把单元格文字画成标签. 颜色取 ROLE_COLOR 角色"""

    def paint(self, painter: QPainter, opt, idx) -> None:
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, idx)
        text = o.text
        o.text = ""                                  # 文字我们自己画, 先让底纹画干净
        st = o.widget.style() if o.widget else QApplication.style()
        st.drawControl(QStyle.CE_ItemViewItem, o, painter, o.widget)
        if not text:
            return

        color = QColor(idx.data(ROLE_COLOR) or "#8a8f98")
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        fm = painter.fontMetrics()
        w = fm.horizontalAdvance(text) + PAD_X * 2
        h = min(fm.height() + PAD_Y * 2, opt.rect.height() - 4)
        # 居中: 这几列是定宽的, 标签居中才跟居中的表头对齐
        r = QRectF(opt.rect.left() + (opt.rect.width() - w) / 2,
                   opt.rect.top() + (opt.rect.height() - h) / 2, w, h)
        painter.setPen(QPen(_edge(color), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        painter.setPen(QPen(color))
        painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self, opt, idx):
        s = super().sizeHint(opt, idx)
        s.setWidth(s.width() + PAD_X * 2 + LEFT)
        return s


class Pill(QLabel):
    """单个标签控件"""

    def __init__(self, text: str, color: str, parent=None):
        super().__init__(text, parent)
        self._color = QColor(color)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(22)
        self.setStyleSheet(f"color:{self._color.name()}; padding:1px 9px;")

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(_edge(self._color), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                          RADIUS, RADIUS)
        p.end()
        super().paintEvent(ev)


class PillRow(QWidget):
    """列表项: 卡 id + 名字 + 状态标签"""

    def __init__(self, card, name: str, status_text: str, color: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(10)
        cid = QLabel(str(card))
        cid.setObjectName("Muted")
        cid.setFixedWidth(48)
        lay.addWidget(cid)
        nm = QLabel(name)
        lay.addWidget(nm, 1)
        if status_text:
            lay.addWidget(Pill(status_text, color))