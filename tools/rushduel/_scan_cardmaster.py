"""扫最大的 N 个 LocalData bundle，找带 passcode 字段的 MonoBehaviour。

用法:
    python _scan_cardmaster.py <游戏目录> [账号]
账号不给就取 LocalData 下第一个。也可以用环境变量 MDSTUDIO_GAME / MDSTUDIO_ACCOUNT。
"""
import os
import sys
from pathlib import Path

import UnityPy


def _game_root() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    env = os.environ.get("MDSTUDIO_GAME")
    if env:
        return Path(env)
    raise SystemExit("用法: python _scan_cardmaster.py <游戏目录> [账号]")


def _account(game: Path) -> str:
    if len(sys.argv) > 2:
        return sys.argv[2]
    env = os.environ.get("MDSTUDIO_ACCOUNT")
    if env:
        return env
    found = sorted(p.parent.name for p in (game / "LocalData").glob("*/0000") if p.is_dir())
    if not found:
        raise SystemExit(f"{game} 下没有 LocalData/<账号>/0000")
    return found[0]


G = _game_root()
ACCT = G / "LocalData" / _account(G)


def main(N=20):
    files = [f for f in ACCT.rglob("*") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_size, reverse=True)
    for f in files[:N]:
        try:
            env = UnityPy.load(str(f))
        except Exception as e:
            continue
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            if not isinstance(tt, dict):
                continue
            keys = list(tt.keys())
            low = " ".join(keys).lower()
            if "passcode" in low or "m_cardid" in low or "cardid" in low:
                print(f"=== HIT {f.name} ({f.stat().st_size/1e6:.1f}MB) ===")
                print("keys:", keys[:40])
                for k in keys[:8]:
                    v = tt.get(k)
                    s = str(v)
                    if len(s) > 80: s = s[:80] + "..."
                    print(f"   {k} = {s}")
                return
        # also scan TextAssets for the word passcode
        for o in env.objects:
            if o.type.name == "TextAsset":
                try:
                    s = o.read().m_Script
                    raw = s if isinstance(s, bytes) else s.encode("latin1")
                except Exception:
                    continue
                if b"passcode" in raw[:500000].lower() or b"44508094" in raw:
                    print(f"=== TextAsset passcode in {f.name} ===")
                    return
    print("no passcode found in top", N)


if __name__ == "__main__":
    main()
