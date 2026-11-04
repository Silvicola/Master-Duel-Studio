"""python -m mdstudio.gui / pythonw 启动用. import 失败也要弹窗, pythonw 下没有控制台"""
import sys

# pythonw 没有控制台, stdout/stderr 可能是 None. 不补上的话 warnings 模块、
# 第三方库的进度条 (tqdm) 一写就炸. 干重活的子进程同理, 见 worker._child
from mdstudio import ensure_std_streams
ensure_std_streams()

try:
    from mdstudio.gui.app import main
except Exception as e:  # 一般是缺 PySide6 或者版本不对
    import ctypes
    import traceback
    ctypes.windll.user32.MessageBoxW(
        None, f"{type(e).__name__}: {e}\n\n{traceback.format_exc(limit=3)}\n\nRun:  pip install .[gui]",
        "MD Cut-in Kit can't start", 0x10)
    sys.exit(1)

sys.exit(main())
