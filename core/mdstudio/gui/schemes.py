"""视频生成方案的存取

一条方案就是一组**画面参数** (帧数/像素/时长/尺寸/偏移/旋转/倍率/曲线).
模式和锐化不归方案管: 它们在图里/图外决定的是"怎么处理", 跟画面摆位无关.
内置方案一始终存在, 用户另存的方案落在**程序目录下的 config/schemes.json**,
这样整个文件夹搬到别的机器上, 方案跟着一起走.
(历史记录和文件备份不在这里: 它们是按机器算的, 而且体积可能有几百 MB.)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mdstudio import runtime
from mdstudio.i18n import tr

# 一个方案能记住的字段, 和界面控件一一对应
FIELDS = ("frames", "cell", "dur", "size", "offx", "offy", "rot", "zoom",
          "preset", "curve", "pop")


def _file() -> Path:
    return runtime.config_dir() / "schemes.json"


def _legacy_file() -> Path:
    """旧位置: 以前存在 LOCALAPPDATA 下, 读到就搬到 config 里"""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MDStudio" / "schemes.json"


def builtin() -> dict:
    """内置方案一: 72 帧 / 1.6 秒 / 角色尺寸 2560 / 倍率 1.0"""
    return {"name": tr("Default"), "frames": 72, "cell": 1820, "dur": 1.6,
            "size": 2560, "offx": 0, "offy": 0, "rot": 0, "zoom": 1.0,
            "preset": 0, "curve": "", "pop": True}


def load() -> list[dict]:
    """内置方案 + 用户存的方案. 文件缺失或坏掉就当没有, 不能让界面起不来"""
    out = [builtin()]
    p = _file()
    if not p.is_file():
        # 老版本存在 LOCALAPPDATA 里, 一次性搬过来
        old = _legacy_file()
        if old.is_file():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        # 老版本存的方案缺字段时用内置值补上, 不至于读一半就炸
        out.append({**builtin(), **{k: item[k] for k in FIELDS + ("name",) if k in item}})
    return out


def save_custom(items: list[dict]) -> None:
    """只落盘用户方案 (内置那条不写, 免得以后改内置值被旧文件盖回去)"""
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [{k: s[k] for k in FIELDS + ("name",) if k in s} for s in items]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
