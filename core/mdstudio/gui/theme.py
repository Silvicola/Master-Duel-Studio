"""Fluent (WinUI 3) 主题 token 与 QSS

颜色值直接对齐 WinUI 3 的 theme resources:
    TextFillColorPrimary / Secondary / Tertiary
    ControlFillColorDefault / Secondary / Tertiary
    ControlStrokeColorDefault / Secondary
    CardBackgroundFillColorDefault / LayerFillColorDefault
    DividerStrokeColorDefault

规矩:
  * alpha 一律写 0-255 整数. QSS 的 rgba() 吃小数时行为不明确 (0.5 与 0.045
    实测会落到同一个值), 用整数没有解释空间.
  * 圆角: 控件 4px, 表面 8px. 这是 Fluent 的 ControlCornerRadius / OverlayCornerRadius.
  * 字体 Segoe UI Variable Text, 正文 14px, 强调用 SemiBold 而不是加粗.

跟随系统用 Qt 自带能力, 不读注册表:
  深浅色  QStyleHints.colorScheme()
  强调色  QPalette.Accent
拿不到就退回 WinUI 默认蓝, 不让界面起不来.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPalette, QPixmap

# WinUI 默认强调色 (Windows 蓝). 系统读不到时用它
FALLBACK_ACCENT = "#0078d4"

FONT_UI = "Segoe UI Variable Text"
FONT_ICON = "Segoe Fluent Icons"
FONT_MONO = "Cascadia Mono"


@dataclass(frozen=True)
class Tokens:
    name: str
    solid: str          # 材质不可用时的兜底底色 (SolidBackgroundFillColorBase)
    layer: str          # 内容区分层 (LayerFillColorDefault)
    card: str           # 卡片 (CardBackgroundFillColorDefault)
    card_hover: str
    stroke: str         # ControlStrokeColorDefault
    stroke_strong: str  # ControlStrokeColorSecondary, 控件底边与勾选框用
    divider: str        # DividerStrokeColorDefault
    edge: str           # 窗口外沿 1px, 浅色壁纸上没有它会糊进背景
    text: str
    muted: str          # TextFillColorSecondary
    text3: str          # TextFillColorTertiary, 占位符与禁用
    field: str          # 输入框底
    inset: str          # 比底色更深的凹陷面, 日志区用
    sel: str
    hover: str          # SubtleFillColorSecondary
    pressed: str        # SubtleFillColorTertiary
    danger_bg: str
    danger_line: str
    danger_text: str


LIGHT = Tokens(
    name="light",
    solid="#f3f3f3",
    layer="rgba(255, 255, 255, 128)",
    card="rgba(255, 255, 255, 178)",
    card_hover="rgba(249, 249, 249, 140)",
    stroke="rgba(0, 0, 0, 15)",
    stroke_strong="rgba(0, 0, 0, 41)",
    divider="rgba(0, 0, 0, 20)",
    edge="rgba(0, 0, 0, 36)",
    text="rgba(0, 0, 0, 229)",
    muted="rgba(0, 0, 0, 155)",
    text3="rgba(0, 0, 0, 114)",
    field="rgba(255, 255, 255, 178)",
    inset="rgba(246, 246, 246, 200)",
    sel="rgba(0, 0, 0, 12)",
    hover="rgba(0, 0, 0, 10)",
    pressed="rgba(0, 0, 0, 6)",
    danger_bg="rgba(196, 43, 28, 20)",
    danger_line="rgba(196, 43, 28, 82)",
    danger_text="#a4262c",
)

DARK = Tokens(
    name="dark",
    solid="#202020",
    layer="rgba(58, 58, 58, 76)",
    card="rgba(255, 255, 255, 13)",
    card_hover="rgba(255, 255, 255, 21)",
    stroke="rgba(0, 0, 0, 26)",
    stroke_strong="rgba(255, 255, 255, 24)",
    divider="rgba(255, 255, 255, 21)",
    edge="rgba(255, 255, 255, 26)",
    text="#ffffff",
    muted="rgba(255, 255, 255, 200)",
    text3="rgba(255, 255, 255, 139)",
    field="rgba(255, 255, 255, 15)",
    inset="rgba(0, 0, 0, 64)",
    sel="rgba(255, 255, 255, 31)",
    hover="rgba(255, 255, 255, 15)",
    pressed="rgba(255, 255, 255, 11)",
    danger_bg="rgba(196, 43, 28, 56)",
    danger_line="rgba(196, 43, 28, 115)",
    danger_text="#f2b8b5",
)


def system_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except (AttributeError, RuntimeError):
        return True


def accent() -> str:
    app = QGuiApplication.instance()
    if app is not None:
        role = getattr(QPalette, "Accent", None)
        if role is not None:
            c = app.palette().color(role)
            if c.isValid():
                return c.name()
    return FALLBACK_ACCENT


def tokens() -> Tokens:
    return DARK if system_dark() else LIGHT


def on_accent() -> str:
    """强调色上的文字. 亮蓝配黑字, 深蓝配白字, 按亮度分"""
    c = QColor(accent())
    lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return "#000000" if lum > 0.55 else "#ffffff"


_RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)")


def qcolor(s: str, alpha: int = None) -> QColor:
    """token 字符串转 QColor

    必须走这里. QColor 不认 "rgba(r, g, b, a)" 这种 CSS 写法, 实测 isValid() 为 False,
    拿去画就是纯黑 (自绘按钮的悬停底色、徽章底色都栽在这上面过).
    """
    m = _RGBA.match((s or "").strip())
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        a = m.group(4)
        if a is None:
            av = 255
        elif "." in a:                      # 0-1 小数按比例换算
            av = round(float(a) * 255)
        else:
            av = int(a)
        c = QColor(r, g, b, max(0, min(255, av)))
    else:
        c = QColor(s)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def apply_font(app) -> None:
    """WinUI 的正文是 14px. Qt 用 pt, 10pt 在 96 DPI 下约 13.3px, 视觉最接近"""
    f = QFont(FONT_UI, 10)
    f.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(f)


def apply_translation(app) -> None:
    """让 Qt 自带的标准按钮 (确定/取消/是/否) 跟随界面语言

    QMessageBox 的按钮文字由 Qt 自己出, 不走 tr(). 不装这个包, 中文界面里
    对话框永远是英文的 OK / Yes / No.
    """
    from mdstudio import i18n
    if i18n.LANG != "zh":
        return
    from PySide6.QtCore import QLibraryInfo, QTranslator

    tr_qt = QTranslator(app)
    if tr_qt.load("qt_zh_CN", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(tr_qt)
        app._qt_translator = tr_qt        # 被回收就失效, 挂在 app 上活到进程结束


_GLYPH_CACHE: dict[tuple, str] = {}


def glyph_image(char: str, color: str, size: int = 16) -> str:
    """把 Segoe Fluent Icons 的字形渲染成 PNG, 返回 QSS 能用的路径

    QSS 画不了矢量. 勾选框的对勾与下拉箭头只能给图, 否则就是一个纯色方块.
    """
    key = (char, color, size)
    if key in _GLYPH_CACHE:
        return _GLYPH_CACHE[key]
    import os
    import tempfile

    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont(FONT_ICON)
    f.setPixelSize(int(size * 0.8))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(px.rect(), Qt.AlignCenter, char)
    p.end()

    name = f"mdstudio-g-{ord(char):x}-{color.lstrip('#')}-{size}.png"
    path = os.path.join(tempfile.gettempdir(), name)
    px.save(path, "PNG")
    _GLYPH_CACHE[key] = path.replace("\\", "/")
    return _GLYPH_CACHE[key]


def check_image(color: str) -> str:
    return glyph_image("\ue73e", color, 16)          # Check


def _shade(hex_color: str, factor: float) -> str:
    """factor < 1 变暗, > 1 变亮"""
    c = QColor(hex_color)
    if factor < 1:
        c = c.darker(int(100 / max(factor, 0.05)))
    else:
        c = c.lighter(int(100 * factor))
    return c.name()


def qss(t: Tokens, material: str) -> str:
    """material 是 backdrop.apply() 的返回值; solid 时才给自己画底"""
    root_bg = "transparent" if material != "solid" else t.solid
    a = accent()
    a_hover = _shade(a, 0.92)
    a_press = _shade(a, 0.82)
    fg = on_accent()
    c = QColor(a)
    a_soft = f"rgba({c.red()}, {c.green()}, {c.blue()}, 26)"
    check = check_image(fg)
    chevron = glyph_image("\ue70d", t.muted, 16)      # ChevronDown
    return f"""
QWidget {{
    color: {t.text};
    font-family: "{FONT_UI}", "Segoe UI", sans-serif;
    font-size: 10pt;
    background: transparent;
}}
QWidget#Root {{
    background: {root_bg};
    border: 1px solid {t.edge};
}}

/* ---- 表面 ---- */
QWidget#Card {{
    background: {t.card};
    border: 1px solid {t.stroke};
    border-radius: 8px;
}}
QGroupBox {{
    background: {t.card};
    border: 1px solid {t.stroke};
    border-radius: 8px;
    margin-top: 0;
    padding: 34px 12px 12px 12px;   /* 顶上这段留给标题, 标题画在卡片内部 */
}}
QGroupBox::title {{
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 14px;
    top: 10px;
    padding: 0;
    color: {t.muted};
    font-size: 10pt;
    font-weight: 600;
}}

/* ---- 字体层级 ---- */
QLabel#Muted {{ color: {t.muted}; }}
QLabel#CardTitle {{ color: {t.muted}; font-size: 10pt; font-weight: 600; }}
/* 卡片内部的列表: 不要再套一层边框 */
QListWidget#Flat {{
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
}}
QLabel#H1 {{ font-size: 20pt; font-weight: 600; }}
QLabel#H2 {{ font-size: 11pt; font-weight: 600; }}
QLabel#Caption {{ color: {t.muted}; font-size: 9pt; }}
QLabel#Version {{ color: {t.muted}; font-size: 10pt; }}
QLabel#Status {{ font-weight: 600; }}

/* ---- 左侧导航 ---- */
QListWidget#Nav {{
    background: transparent;
    border: none;
    outline: none;
    padding: 4px 2px;
}}
QListWidget#Nav::item {{
    color: {t.muted};
    border-radius: 4px;
    padding-left: 10px;
    border-left: 3px solid transparent;
    margin: 1px 0;
}}
QListWidget#Nav::item:hover {{
    background: {t.hover};
    color: {t.text};
}}
QListWidget#Nav::item:selected {{
    background: {t.pressed};
    color: {t.text};
    border-left: 3px solid {a};
    font-weight: 600;
}}

/* ---- 列表与表格 ---- */
QListWidget {{
    background: {t.card};
    border: 1px solid {t.stroke};
    border-radius: 8px;
    outline: none;
    padding: 2px;
}}
QListWidget::item {{
    border-radius: 4px;
    padding: 5px 8px;
    color: {t.text};
}}
QListWidget::item:hover {{ background: {t.hover}; }}
QListWidget::item:selected {{
    background: {t.sel};
    color: {t.text};
}}
QTableWidget {{
    background: {t.card};
    border: 1px solid {t.stroke};
    border-radius: 8px;
    gridline-color: transparent;
    outline: none;
}}
QTableWidget::item {{ padding: 4px 8px; }}
QTableWidget::item:hover {{ background: {t.hover}; }}
QTableWidget::item:selected {{ background: {t.sel}; color: {t.text}; }}
QHeaderView::section {{
    background: transparent;
    color: {t.muted};
    font-size: 9pt;
    border: none;
    border-right: 1px solid {t.divider};
    border-bottom: 1px solid {t.divider};
    padding: 7px 8px;
}}
QTableCornerButton::section {{ background: transparent; border: none; }}
/* 卡片内部的表格: 不要再套一层边框 */
QTableWidget#Flat {{
    background: transparent;
    border: none;
    border-radius: 0;
}}

/* ---- 按钮 ---- */
QPushButton {{
    background: {t.card};
    border: 1px solid {t.stroke};
    border-bottom-color: {t.stroke_strong};
    border-radius: 4px;
    padding: 4px 12px;
    min-height: 22px;
    color: {t.text};
}}
QPushButton:hover {{ background: {t.card_hover}; }}
QPushButton:pressed {{
    background: {t.pressed};
    border-bottom-color: {t.stroke};
    color: {t.muted};
}}
QPushButton:disabled {{
    background: {t.pressed};
    border-color: {t.stroke};
    color: {t.text3};
}}
QPushButton#Primary {{
    background: {a};
    border: 1px solid {a};
    border-bottom-color: {a_press};
    color: {fg};
}}
QPushButton#Primary:hover {{ background: {a_hover}; border-color: {a_hover}; }}
/* #Primary 的 ID 比 :disabled 更特指, 不补这条禁用态还是亮的 */
QPushButton#Primary:disabled {{
    background: {t.pressed};
    border: 1px solid {t.stroke};
    color: {t.text3};
}}
QPushButton#Primary:pressed {{ background: {a_press}; border-color: {a_press}; }}
QPushButton#Danger {{
    background: {t.danger_bg};
    border: 1px solid {t.danger_line};
    color: {t.danger_text};
}}
/* 分段切换: 未选透明, 选中给卡片底与加重文字 */
QPushButton#Seg {{
    background: transparent;
    border: 1px solid {t.stroke};
    border-radius: 4px;
    padding: 4px 14px;
    min-height: 22px;
    color: {t.muted};
}}
QPushButton#Seg:hover {{ background: {t.hover}; color: {t.text}; }}
QPushButton#Seg:checked {{
    background: {t.card};
    border: 1px solid {t.stroke_strong};
    color: {t.text};
    font-weight: 600;
}}

/* ---- 输入 ---- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {t.field};
    border: 1px solid {t.stroke};
    border-bottom: 1px solid {t.stroke_strong};
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 22px;
    selection-background-color: {a};
    selection-color: {fg};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-bottom: 2px solid {a};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {t.text3};
    border-color: {t.stroke};
}}
/* 只读 = 值由别处决定 (如官方卡的来源固定成它自己). 底压暗一档, 让人看得出改不了 */
QLineEdit:read-only {{
    background: {t.pressed};
    color: {t.muted};
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{
    image: url({chevron});
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background: {t.solid};
    border: 1px solid {t.divider};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: {a};
    selection-color: {fg};
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid {t.stroke_strong};
    background: {t.field};
}}
QCheckBox::indicator:hover {{ border-color: {t.muted}; }}
QCheckBox::indicator:checked {{
    background: {a};
    border-color: {a};
    image: url({check});
}}
QCheckBox::indicator:disabled {{
    background: {t.pressed};
    border-color: {t.stroke};
}}

QPlainTextEdit#Console {{
    background: {t.inset};
    border: 1px solid {t.stroke};
    border-radius: 8px;
    padding: 8px;
    color: {t.muted};
    font-family: "{FONT_MONO}", Consolas, monospace;
    font-size: 9pt;
}}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:vertical {{ height: 8px; }}

/* ---- 滚动条 ---- */
/* 轨道给一点底色: 全透明时这块区域看着像"表格右边缺了一块" */
/* 轨道用 hover 那档底色才看得见 (inset 太淡); handle 加宽到 6px */
QScrollBar:vertical {{ background: {t.hover}; width: 12px; margin: 2px; border-radius: 6px; }}
QScrollBar::handle:vertical {{
    background: {t.text3};
    border-radius: 3px;
    min-height: 28px;
    margin: 0 3px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.muted}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {t.text3};
    border-radius: 3px;
    min-width: 28px;
    margin: 4px 0;
}}
QScrollBar::handle:horizontal:hover {{ background: {t.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}

QProgressBar {{
    background: {t.hover};
    border: none;
    border-radius: 2px;
    min-height: 4px;
    max-height: 4px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {a}; border-radius: 2px; }}

/* 可折叠区块的标题行: 只留文字和箭头, 不要按钮框 */
QPushButton#Expander {{
    background: transparent;
    border: none;
    padding: 4px 2px;
    color: {t.muted};
    font-weight: 600;
    text-align: left;
}}
QPushButton#Expander:hover {{ color: {t.text}; }}

/* ---- 对话框 ---- */
/* 顶层 QWidget 是 transparent, 对话框继承到这条就会画成黑底 (透明落在无背景的
   普通窗口上等于黑). 必须显式给不透明底色 */
QDialog, QMessageBox, QInputDialog {{
    background: {t.solid};
}}
QMessageBox QLabel, QDialog QLabel, QInputDialog QLabel {{
    background: transparent;
    color: {t.text};
}}
QMessageBox QPushButton {{
    min-width: 72px;
}}

QToolTip {{
    background: {t.solid};
    color: {t.text};
    border: 1px solid {t.divider};
    border-radius: 4px;
    padding: 5px 8px;
}}
"""