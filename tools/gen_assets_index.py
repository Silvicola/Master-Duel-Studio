"""生成 assets_index.json：把随包素材的文件清单（路径 + 大小 + sha256）固化下来。

打包时用 --slim 把这些目录剔出 zip，用户首次启动按这份清单逐个下载。
清单本身很小（几 KB），跟代码一起进包。

用法: python tools/gen_assets_index.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core" / "mdstudio" / "assets_index.json"

# 目录 -> 仓库里的相对路径（raw 下载的 base 由 assets.py 拼）
GROUPS = {
    "spineconv": "tools/spineconv",
}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    out = {}
    for key, rel in GROUPS.items():
        base = ROOT / rel
        files = []
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            files.append([p.relative_to(base).as_posix(), p.stat().st_size, sha(p)])
        out[key] = files
        print(f"{key}: {len(files)} 个文件")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"写出 {OUT}  {OUT.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
