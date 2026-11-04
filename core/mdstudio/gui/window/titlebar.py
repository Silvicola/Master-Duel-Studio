"""自绘标题栏 + 原生边框窗口

做法不是 FramelessWindowHint, 而是保留系统边框、在 WM_NCCALCSIZE 里把非客户区
高度吃掉。这样下面这些全部由系统免费提供, 一行都不用自己写:

    拖动窗口 / 双击最大化 / 右键系统菜单 / 拖到屏幕边缘贴靠 / Win+方向键

我们只补两件事:
    WM_NCHITTEST   标题栏区域返回 HTCAPTION, 最大化键返回 HTMAXBUTTON
    WM_NCLBUTTONDOWN  wParam 是 HTMAXBUTTON 时自己切换最大化 (Snap 菜单仍由系统弹)

最大化时客户区等于窗口矩形, 而窗口矩形比屏幕大一圈边框, 所以要用边框厚度做内边距,
否则内容会被裁掉 8 像素。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMainWindow, QVBoxLayout, QWidget)

from mdstudio.gui import theme
from mdstudio.gui.window import backdrop

TITLEBAR_H = 44
BTN_W = 46
BTN_H = 32
GLYPH = 10
BORDER = 6          # 窗口边缘可拖拽缩放的宽度 (逻辑像素)

HTCLIENT = 1
HTCAPTION = 2
HTMAXBUTTON = 9
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCACTIVATE = 0x0086
WM_NCMOUSELEAVE = 0x02A2
WM_STYLECHANGING = 0x007C

GWL_STYLE = -16
WS_CAPTION = 0x00C00000


class _STYLESTRUCT(ctypes.Structure):
    _fields_ = [("styleOld", wintypes.DWORD), ("styleNew", wintypes.DWORD)]

CLOSE_RED = "#c42b1c"


class CaptionButton(QWidget):
    """最小化 / 最大化 / 关闭. 用 QWidget 自绘, 不用 QPushButton 是为了不吃 QSS 的边框"""

    clicked = Signal()

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.hover = False
        self.maximized = False
        self.fg = QColor("#e8e8e8")
        self.hover_bg = QColor(255, 255, 255, 24)
        self.setFixedSize(BTN_W, BTN_H)
        self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()

    def enterEvent(self, ev):
        self.hover = True
        self.update()

    def leaveEvent(self, ev):
        self.hover = False
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.hover:
            bg = QColor(CLOSE_RED) if self.kind == "close" else self.hover_bg
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 4, 4)

        fg = QColor("#ffffff") if (self.hover and self.kind == "close") else self.fg
        p.setPen(QPen(fg, 1.2))
        cx, cy = self.width() / 2, self.height() / 2
        g = GLYPH / 2
        if self.kind == "min":
            p.drawLine(QPoint(int(cx - g), int(cy)), QPoint(int(cx + g), int(cy)))
        elif self.kind == "max":
            if self.maximized:
                p.drawRect(QRect(int(cx - g), int(cy - g + 3), GLYPH - 3, GLYPH - 3))
                p.drawRect(QRect(int(cx - g + 3), int(cy - g), GLYPH - 3, GLYPH - 3))
            else:
                p.drawRect(QRect(int(cx - g), int(cy - g), GLYPH, GLYPH))
        else:
            p.drawLine(QPoint(int(cx - g), int(cy - g)), QPoint(int(cx + g), int(cy + g)))
            p.drawLine(QPoint(int(cx + g), int(cy - g)), QPoint(int(cx - g), int(cy + g)))
        p.end()


class DryBadge(QWidget):
    """标题栏正中的「预演模式」提示. 点一下去设置页关掉它

    用叠加定位而不是塞进布局: 左右两侧宽度不等, 塞进布局只会得到"差不多中间",
    这里要的是真的居中.
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fg = QColor("#ffffff")
        self._bg = QColor(196, 43, 28, 220)
        self.setCursor(Qt.PointingHandCursor)
        self.hide()

    def set_colors(self, fg: str, bg: str) -> None:
        self._fg, self._bg = theme.qcolor(fg), theme.qcolor(bg)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self._text) + 28, 24)

    _text = ""

    def set_text(self, text: str) -> None:
        self._text = text
        self.setFixedSize(self.sizeHint())
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._bg)
        p.drawRoundedRect(self.rect(), 12, 12)
        p.setPen(QPen(self._fg))
        p.drawText(self.rect(), Qt.AlignCenter, self._text)
        p.end()


class CustomTitleBar(QWidget):
    min_clicked = Signal()
    close_clicked = Signal()
    badge_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(TITLEBAR_H)
        self.setMouseTracking(True)
        self._active = True
        self._tokens = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(8)

        self.title = QLabel("")
        self.title.setObjectName("H2")
        lay.addWidget(self.title)

        self.sub = QLabel("")
        self.sub.setObjectName("Caption")
        lay.addWidget(self.sub)
        lay.addStretch(1)

        self.btn_min = CaptionButton("min", self)
        self.btn_max = CaptionButton("max", self)
        self.btn_close = CaptionButton("close", self)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b, 0, Qt.AlignVCenter)

        self.badge = DryBadge(self)
        self.badge.clicked.connect(self.badge_clicked)

        self.btn_min.clicked.connect(self.min_clicked)
        self.btn_close.clicked.connect(self.close_clicked)
        # 最大化键不连 Qt 信号: 它在 WM_NCHITTEST 里返回 HTMAXBUTTON 交给系统接管,
        # Qt 收不到 mouseReleaseEvent。点击走窗口的 WM_NCLBUTTONDOWN 分支。

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._center_badge()

    def _center_badge(self) -> None:
        if self.badge.isVisible():
            self.badge.move((self.width() - self.badge.width()) // 2,
                            (self.height() - self.badge.height()) // 2)

    def set_subtitle(self, text: str) -> None:
        """标题右侧的「·」加账号, 没有账号时整段隐藏"""
        self.sub.setText(f"·  {text}" if text else "")
        self.sub.setVisible(bool(text))

    def set_badge(self, text: str) -> None:
        if not text:
            self.badge.hide()
            return
        self.badge.set_text(text)
        self.badge.show()
        self.badge.raise_()
        self._center_badge()

    def refresh_colors(self, t: theme.Tokens) -> None:
        self._tokens = t
        self._apply_tone()

    def set_active(self, on: bool) -> None:
        """窗口失焦时标题与按钮变淡, 跟 Win11 原生标题栏一致"""
        if self._active == on:
            return
        self._active = on
        self._apply_tone()

    def _apply_tone(self) -> None:
        t = getattr(self, "_tokens", None)
        if t is None:
            return
        fg = theme.qcolor(t.text if self._active else t.muted)
        hover = theme.qcolor(t.text, 26)
        self.title.setStyleSheet("" if self._active else f"color:{t.muted};")
        self.sub.setStyleSheet("" if self._active else f"color:{t.muted};")
        for b in (self.btn_min, self.btn_max, self.btn_close):
            b.fg = fg
            b.hover_bg = hover
            b.update()
        # 预演是"注意"级状态, 用琥珀色而不是强调蓝: 蓝色读起来像普通选中态
        if t.name == "dark":
            self.badge.set_colors("#1b1b1b", "rgba(255, 185, 0, 235)")
        else:
            self.badge.set_colors("#ffffff", "rgba(157, 93, 0, 235)")

    def set_maximized(self, on: bool) -> None:
        self.btn_max.maximized = on
        self.btn_max.update()

    def set_snap_hover(self, on: bool) -> None:
        if self.btn_max.hover != on:
            self.btn_max.hover = on
            self.btn_max.update()


class MicaWindow(QMainWindow):
    """带 Mica 材质与自绘标题栏的主窗口骨架"""

    dry_badge_clicked = Signal()

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(900, 620)
        self._place_default()

        self._material = "solid"
        self._tokens = theme.tokens()

        root = QWidget()
        root.setObjectName("Root")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._root_lay = lay

        self._bar = CustomTitleBar(root)
        self._bar.title.setText(title)
        lay.addWidget(self._bar)

        self._body = QWidget()
        self._body.setObjectName("Body")
        lay.addWidget(self._body, 1)
        self.setCentralWidget(root)

        self._bar.min_clicked.connect(self.showMinimized)
        self._bar.close_clicked.connect(self.close)
        self._bar.badge_clicked.connect(self.dry_badge_clicked)

        # 只做一次: 去掉 WS_CAPTION, 否则 DWM 会在客户区里再画一套标题栏按钮
        backdrop.strip_caption(int(self.winId()))

        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._refresh)

    def _place_default(self) -> None:
        """默认尺寸不能超过可用屏幕. 200% 缩放的 2K 屏只有 1280×720 可用"""
        scr = QGuiApplication.primaryScreen()
        if scr is None:
            self.resize(1280, 840)
            return
        avail = scr.availableGeometry()
        w = min(1300, int(avail.width() * 0.92))
        h = min(860, int(avail.height() * 0.92))
        self.resize(max(w, 900), max(h, 620))
        self.move(avail.x() + (avail.width() - self.width()) // 2,
                  avail.y() + (avail.height() - self.height()) // 2)

    # --- 给页面用的入口 ---
    def body(self) -> QWidget:
        return self._body

    def set_account_label(self, text: str) -> None:
        """标题栏「· 账号」那一段. 空字符串就整段不显示"""
        self._bar.set_subtitle(text)

    def set_dry_run(self, on: bool) -> None:
        """预演模式: 标题栏正中挂一个可点的提示条"""
        from mdstudio.i18n import tr
        self._bar.set_badge(tr("Dry run mode") if on else "")

    def titlebar(self) -> CustomTitleBar:
        return self._bar

    def material(self) -> str:
        return self._material

    def tokens(self) -> theme.Tokens:
        return self._tokens

    # --- 材质与主题 ---
    def showEvent(self, ev):
        super().showEvent(ev)
        self._refresh()

    def changeEvent(self, ev):
        if ev.type() == QEvent.WindowStateChange:
            self._refresh()
        super().changeEvent(ev)

    def _refresh(self) -> None:
        self._material = backdrop.apply(int(self.winId()), dark=theme.system_dark(),
                                        maximized=self.isMaximized())
        self._tokens = theme.tokens()
        self.setStyleSheet(theme.qss(self._tokens, self._material))
        self._bar.refresh_colors(self._tokens)
        self._bar.set_maximized(self.isMaximized())
        self._fix_max_margin()
        self.on_theme_changed(self._tokens)

    def on_theme_changed(self, t: theme.Tokens) -> None:
        """子类覆盖: 主题切了之后要重画的东西 (图标之类)"""

    def _fix_max_margin(self) -> None:
        """最大化时窗口比工作区大一圈, 用内边距把内容让回来"""
        if not self.isMaximized():
            self._root_lay.setContentsMargins(0, 0, 0, 0)
            return
        dpr = self.devicePixelRatioF() or 1.0
        pad = int(round(backdrop.max_inset(int(self.winId())) / dpr))
        self._root_lay.setContentsMargins(pad, pad, pad, pad)

    def _toggle_max(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # --- 命中测试 ---
    def _rect_of(self, w: QWidget) -> QRect:
        return QRect(w.mapTo(self, QPoint(0, 0)), w.size())

    def _hit(self, sx: int, sy: int) -> int:
        dpr = self.devicePixelRatioF() or 1.0
        pos = self.mapFromGlobal(QPoint(int(round(sx / dpr)), int(round(sy / dpr))))
        if not self.rect().contains(pos):
            return HTCLIENT

        if self._rect_of(self._bar.btn_max).contains(pos):
            self._bar.set_snap_hover(True)
            return HTMAXBUTTON
        self._bar.set_snap_hover(False)

        # 预演徽章在标题栏范围内, 不特判的话会返回 HTCAPTION 被系统当成拖动,
        # 点击根本到不了控件
        if self._bar.badge.isVisible() and \
                self._rect_of(self._bar.badge).contains(pos):
            return HTCLIENT

        if self._rect_of(self._bar.btn_min).contains(pos) or \
                self._rect_of(self._bar.btn_close).contains(pos):
            return HTCLIENT

        b = BORDER
        if not self.isMaximized():
            hit = self._border_hit(pos, b)
            if hit:
                return hit

        if self._rect_of(self._bar).contains(pos):
            return HTCAPTION
        return HTCLIENT

    def _border_hit(self, pos: QPoint, b: int) -> int:
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        left, right = x < b, x >= w - b
        top, bottom = y < b, y >= h - b
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        return 0

    # --- Windows 消息 ---
    def nativeEvent(self, event_type, message):
        if event_type != b"windows_generic_MSG":
            return super().nativeEvent(event_type, message)
        msg = wintypes.MSG.from_address(int(message))

        if msg.message == WM_STYLECHANGING:
            # Qt 在 setWindowFlags / 改属性 / 某些状态切换时会重算样式位, 把
            # WS_CAPTION 加回去, 系统标题栏于是又冒出来 (双标题栏).
            # 在这里改 styleNew 而不是事后再 strip: 事后 strip 会先画一帧系统标题栏.
            if ctypes.c_int(msg.wParam).value == GWL_STYLE:
                s = _STYLESTRUCT.from_address(int(msg.lParam))
                if s.styleNew & WS_CAPTION:
                    s.styleNew &= ~WS_CAPTION
            return super().nativeEvent(event_type, message)

        if msg.message == WM_NCACTIVATE:
            # 必须吞掉. DefWindowProc 处理这条消息时会重画非客户区, 而我们用
            # DwmExtendFrameIntoClientArea 把整窗都声明成"框", 于是它就在客户区里
            # 画出一整套系统标题栏 (图标 + 标题 + 三个按钮), 压在我们自绘的上面.
            # 只在激活状态切换时触发, 所以表现是"切走再回来就变双标题栏".
            self._bar.set_active(bool(msg.wParam))
            return True, 1

        if msg.message == WM_NCCALCSIZE:
            if not msg.wParam:
                return super().nativeEvent(event_type, message)
            # 客户区 = 整个窗口矩形, 系统标题栏就此消失, 边框与阴影保留
            return True, 0

        if msg.message == WM_NCHITTEST:
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            hit = self._hit(x, y)
            if hit == HTCLIENT:
                return super().nativeEvent(event_type, message)
            return True, hit

        if msg.message == WM_NCLBUTTONDOWN and int(msg.wParam) == HTMAXBUTTON:
            self._toggle_max()
            return True, 0

        if msg.message == WM_NCMOUSELEAVE:
            self._bar.set_snap_hover(False)

        return super().nativeEvent(event_type, message)
