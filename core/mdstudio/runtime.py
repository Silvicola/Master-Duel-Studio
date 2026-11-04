"""运行时根

两个根要分清, 打包后它们不是同一个目录:

  app_root()      exe 所在目录. config 与 data 落这里, 用户看得见.
                  冻结后 __file__ 指向解包目录, 所以 exe 路径只能靠 sys.argv[0].
  bundle_root()   随包资源所在目录. models 与 tools 在这里, 用户看不见.
                  冻结后就是 onefile 的解包目录.

源码直接跑时两者都退回项目根, 开发期与打包后的相对路径保持一致.

便携原则: 任何东西都不许写到程序目录之外. 目录不可写就报错让用户挪位置,
不偷偷退到 LOCALAPPDATA —— 那正是这个工具不该碰的地方.
"""
import os
import sys
from pathlib import Path

_WRITABLE = []


def frozen() -> bool:
    """Nuitka 编译后 __compiled__ 存在; PyInstaller 打包后 sys.frozen 为真"""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def exe_path() -> Path:
    """exe 本体路径. 冻结后 __file__ 在解包目录里, 拿不到 exe 位置"""
    return Path(sys.argv[0]).resolve()


def app_root() -> Path:
    if frozen():
        return exe_path().parent
    return Path(__file__).resolve().parents[2]


def bundle_root() -> Path:
    """随包资源目录: 源码是项目根, 冻结后是 onefile 的解包目录 (mdstudio 包的上一层)"""
    if frozen():
        return Path(__file__).resolve().parents[1]
    return Path(__file__).resolve().parents[2]


def writable_root() -> Path:
    """config 与 data 的落点. 不可写直接报错, 不做兜底"""
    if _WRITABLE:
        return _WRITABLE[0]
    root = app_root()
    probe = root / ".write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        raise RuntimeError(
            "程序所在目录不可写: %s\n请把 exe 挪到可写位置 (桌面、数据盘、U 盘) 后再启动。(%s)"
            % (root, e)
        ) from None
    _WRITABLE.append(root)
    return root


def data_dir() -> Path:
    """操作记录与文件备份. MDSTUDIO_HOME 可以指到别的盘"""
    env = os.environ.get("MDSTUDIO_HOME")
    if env:
        return Path(env)
    return writable_root() / "data"


def config_dir() -> Path:
    return writable_root() / "config"
