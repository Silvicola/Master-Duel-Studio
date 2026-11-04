"""窗口材质: Mica / 亚克力 / 纯色, 按系统版本自动降级

全部走系统接口 (dwmapi / user32), 不引第三方库.

    hwnd = int(window.winId())
    level = backdrop.apply(hwnd, dark=True, maximized=False)
    # level: mica / mica-alt / mica-legacy / acrylic / solid

属性号来自 dwmapi.h; 1029 是 Win11 21H2 上的临时号, 22H2 起改用 38.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_MICA_EFFECT = 1029

DWMWCP_DEFAULT = 0
DWMWCP_ROUND = 2

DWMSBT_MAINWINDOW = 2         # Mica
DWMSBT_TABBEDWINDOW = 4       # Mica Alt

WCA_ACCENT_POLICY = 19
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

BUILD_MICA = 22000            # Win11 21H2
BUILD_MICA_NEW = 22621        # Win11 22H2


class _MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


class _ACCENT(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                ("gradient", ctypes.c_uint), ("animation", ctypes.c_int)]


class _ATTRDATA(ctypes.Structure):
    _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p),
                ("size", ctypes.c_size_t)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_dwm = ctypes.windll.dwmapi
_user32 = ctypes.windll.user32

_dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD]
_dwm.DwmSetWindowAttribute.restype = ctypes.c_long
_dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(_MARGINS)]
_dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.SetWindowPos.restype = wintypes.BOOL

MONITOR_DEFAULTTONEAREST = 2

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020


def build() -> int:
    return sys.getwindowsversion().build


def _set(hwnd: int, attr: int, value: int) -> bool:
    v = ctypes.c_int(value)
    return _dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr,
                                      ctypes.byref(v), ctypes.sizeof(v)) == 0


def extend_frame(hwnd: int) -> None:
    """把边框延伸进客户区. 不做这一步 Mica 只出现在标题栏, 客户区还是自己画的"""
    m = _MARGINS(-1, -1, -1, -1)
    _dwm.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(m))


def strip_caption(hwnd: int) -> None:
    """去掉 WS_CAPTION

    延伸边框之后整个客户区都算"框", 只要窗口还带着 WS_CAPTION, DWM 就会把
    系统的三个标题栏按钮也画进来, 于是客户区里出现两套最小化/最大化/关闭.
    去掉这个样式位 DWM 就不画了, 而缩放、阴影、贴靠都还在 (它们靠
    WS_THICKFRAME / WS_MAXIMIZEBOX / WS_SYSMENU, 跟 CAPTION 无关).
    """
    style = _user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_STYLE)
    _user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_STYLE, style & ~WS_CAPTION)
    _user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                         SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)


def set_corner(hwnd: int, rounded: bool = True) -> bool:
    return _set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                DWMWCP_ROUND if rounded else DWMWCP_DEFAULT)


def set_dark(hwnd: int, dark: bool) -> bool:
    return _set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


def max_inset(hwnd: int) -> int:
    """最大化时窗口矩形比工作区每边大多少 (物理像素)

    客户区等于窗口矩形, 所以要多出来的这部分当内边距, 否则内容被屏幕裁掉.
    Windows 11 的细边框实测是固定 6 像素, 但 GetSystemMetricsForDpi 报的是
    96 DPI 的值 (4+1). 直接量窗口与工作区之差比自己算边框可靠.
    """
    r = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return 0
    mon = _user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return 0
    work = info.rcWork
    return max(0, ((r.right - r.left) - (work.right - work.left)) // 2)


def _acrylic(hwnd: int, dark: bool) -> bool:
    """Win10 1809+ 的亚克力. 渐变值格式是 AABBGGRR"""
    fn = getattr(_user32, "SetWindowCompositionAttribute", None)
    if fn is None:
        return False
    accent = _ACCENT(ACCENT_ENABLE_ACRYLICBLURBEHIND, 2,
                     0xCC1A1A1A if dark else 0xCCEAEAEA, 0)
    data = _ATTRDATA(WCA_ACCENT_POLICY,
                     ctypes.cast(ctypes.byref(accent), ctypes.c_void_p),
                     ctypes.sizeof(accent))
    fn(wintypes.HWND(hwnd), ctypes.byref(data))
    return True


def apply(hwnd: int, dark: bool, maximized: bool = False) -> str:
    """按系统版本挑一个能用上的档位, 返回档位名供界面显示与排错"""
    b = build()
    extend_frame(hwnd)
    set_corner(hwnd, rounded=not maximized)
    set_dark(hwnd, dark)
    if b >= BUILD_MICA_NEW:
        want = DWMSBT_TABBEDWINDOW if maximized else DWMSBT_MAINWINDOW
        if _set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, want):
            return "mica-alt" if maximized else "mica"
    if b >= BUILD_MICA:
        if _set(hwnd, DWMWA_MICA_EFFECT, 1):
            return "mica-legacy"
    if _acrylic(hwnd, dark):
        return "acrylic"
    return "solid"


def describe(level: str) -> str:
    return {"mica": "Mica", "mica-alt": "Mica Alt", "mica-legacy": "Mica (21H2)",
            "acrylic": "亚克力", "solid": "纯色"}.get(level, level)
