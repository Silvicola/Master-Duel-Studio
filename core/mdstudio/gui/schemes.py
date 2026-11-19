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


# 两个内置方案（不落盘，改代码就是新值）。角色尺寸 2930 的来历（2026-11-12 游戏内实测）：
# 1 尺寸单位 ≈ 屏宽/5000 屏幕像素，角色屏高 ≈ 0.96 × 尺寸 × 屏宽/5000 × 弹出缩放。
# 1440p 下 2930 的峰值（缩放 1.10）是 110% 屏高（1584px），稳定段（0.75~0.85）75~85%。
# 旧值 4260 是按「屏高/10」换算出来的，差一个 16:9，实机是 150% 屏高（头脚都切）。
# 像素/帧跟的是「角色在屏幕上占多少像素」，跟尺寸无关。
#
# 2026-11-19 定稿：两档，都开弹出。弹出曲线不放在方案里 —— 后端的默认曲线
# （cutin_flip.DEFAULT_POP_CURVE）就是用户自己设计的那套：25% 淡入起步、冲到 108%、
# 砸到 76% 收回、缓回 90% 收尾。它按曲线自己的最后一帧归一化，任何帧数都对得上，
# 所以两档不需要各存一份曲线字符串，「效果」下拉里选「默认」即可。
#   低（一页）48 帧 @1800：7×7 排一页（边长 12600），约 158 MB，30fps 播放。
#     1800 是按「峰值 1760px」（2K 16:10 上 110% 屏高）反推的，再低那 0.1 秒会糊；
#     48 帧排一页的上限其实是 2340（7×2340=16380），1800 留了充足余量。
#   高（两页）72 帧 @2560：6×6 排两页（边长 15360），约 471 MB，45fps 播放。
#
# 名字带页数是给用户看的：选了「两页」就该知道会写两张图集。用「贴图」不用「纹理」，
# 跟报错文案（"只有一页贴图"）一致；不带「方案」二字，是因为下拉框窄，长了会被截断。
BUILTINS = (
    ("低（一页）", 48, 1800),
    ("高（两页）", 72, 2560),
)
_SIZE = 2930


def _base() -> dict:
    return {"frames": 72, "cell": 1664, "dur": 1.6, "size": _SIZE,
            "offx": 0, "offy": 0, "rot": 0, "zoom": 1.0,
            "preset": 0, "curve": "", "pop": True}


def builtins() -> list[dict]:
    """内置方案：低（一页）在前、高（两页）在后。界面默认用第一个。"""
    return [{**_base(), "name": name, "frames": frames, "cell": cell, "builtin": True}
            for name, frames, cell in BUILTINS]


def load() -> list[dict]:
    """内置方案 + 用户存的方案. 文件缺失或坏掉就当没有, 不能让界面起不来"""
    out = builtins()
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
        out.append({**_base(), "builtin": False,
                    **{k: item[k] for k in FIELDS + ("name",) if k in item}})
    return out


def save_custom(items: list[dict]) -> None:
    """只落盘用户方案 (内置那几条不写, 免得以后改内置值被旧文件盖回去)"""
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [{k: s[k] for k in FIELDS + ("name",) if k in s}
            for s in items if not s.get("builtin")]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
