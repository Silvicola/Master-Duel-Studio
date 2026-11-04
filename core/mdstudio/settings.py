"""用户设置: 游戏目录与选中的账号

落在 config/settings.json, 跟程序目录一起搬走. 读失败一律当没设置,
不能让配置文件坏掉就把界面卡死.

字段:
    game_dir   游戏根目录 (含 LocalData 的那一层)
    account    LocalData/<account>/0000 里的 <account>
    names      账号备注名 {"<account>": "主号"}, 存档是加密的取不到玩家名, 只能自己起
"""
from __future__ import annotations

import json
from pathlib import Path

from mdstudio import runtime

DEFAULTS: dict = {"game_dir": "", "account": "", "names": {}}


def path() -> Path:
    return runtime.config_dir() / "settings.json"


def load() -> dict:
    out = {"game_dir": "", "account": "", "names": {}}
    f = path()
    if not f.is_file():
        return out
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(raw, dict):
        for k in ("game_dir", "account"):
            v = raw.get(k)
            if isinstance(v, str):
                out[k] = v
        names = raw.get("names")
        if isinstance(names, dict):
            out["names"] = {str(a): str(n) for a, n in names.items() if n}
    return out


def get(key: str, default=None):
    return load().get(key, default)


def save(**kw) -> None:
    cur = load()
    for k, v in kw.items():
        if k in ("game_dir", "account"):
            cur[k] = "" if v is None else str(v)
        elif k == "names" and isinstance(v, dict):
            cur["names"] = {str(a): str(n) for a, n in v.items() if n}
    f = path()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        pass          # 写不了就算了, 设置只在下次启动时丢, 不影响当前会话


def account_name(account: str) -> str:
    return load()["names"].get(account, "")


def set_account_name(account: str, name: str) -> None:
    names = dict(load()["names"])
    name = (name or "").strip()
    if name:
        names[account] = name
    else:
        names.pop(account, None)      # 清空就删掉, 不留空串
    save(names=names)


def label(account: str) -> str:
    """标题栏用的账号文字: 哈希 + 可选备注名"""
    name = account_name(account)
    return f"{account} · {name}" if name else account


def valid_account(game_dir, account: str) -> bool:
    """账号真的存在才认, 否则当没选 (换机器/换目录后旧设置会失效)"""
    if not account:
        return False
    return (Path(game_dir) / "LocalData" / account / "0000").is_dir()