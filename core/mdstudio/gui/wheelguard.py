"""滚轮保护: 别让滚动页面时顺手改掉下拉框和数字框的值

现象: 鼠标停在「帧数 / 像素每帧 / 尺寸」这类数字框上滚页面, 值跟着变.
这些参数一旦被悄悄改掉, 生成出来的动画尺寸就是错的, 而且很难察觉.

做法: 应用级事件过滤器, 拦下拉框与数字框的滚轮, 把事件转交给外层滚动区.
于是滚轮只滚页面, 值一个都不动; 要改值用箭头键或直接点选.
下拉框弹开时例外, 那时候滚轮本来就该滚选项.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (QAbstractScrollArea, QAbstractSpinBox, QApplication,
                               QComboBox)


def _scroll_area_of(w):
    """往上找最近的滚动区, 没有就返回 None (页面本身不滚)"""
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QAbstractScrollArea):
            return p
        p = p.parentWidget()
    return None


class WheelGuard(QObject):
    def eventFilter(self, obj, ev) -> bool:
        if ev.type() != QEvent.Wheel:
            return False
        if not isinstance(obj, (QComboBox, QAbstractSpinBox)):
            return False
        if isinstance(obj, QComboBox) and obj.view().isVisible():
            return False                     # 弹开的列表: 滚轮滚选项, 别拦

        area = _scroll_area_of(obj)
        if area is not None:
            QApplication.sendEvent(area.viewport(), ev)   # 页面照常滚动
        return True                          # 无论有没有滚动区, 都不让控件吃这条滚轮


def install(app) -> WheelGuard:
    guard = WheelGuard(app)
    app.installEventFilter(guard)
    return guard