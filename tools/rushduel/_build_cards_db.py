"""构建 Rush Duel 用的卡牌数据库，按游戏内部 id 索引。

数据来源（构建期一次性联网/本地取，产物随包发布，运行期离线）：
- koishipro 中文卡库 `_cn.cdb`（texts: 中文名 -> 卡号/passcode）做 MD 内部 id 的桥。
  MD 卡名（Konami 简中）与 OCG 简中（萌卡）多数一致，少数因本地化用词不同；
  用 NFKC + 去“的/之/标点”归一 + 模糊匹配兜底。
- YGOPRODeck 原始 dump `_ygo_raw.json`（passcode -> type/race/attribute/level/scale/linkval/
  frameType/pend_desc），提供属性/种族/等级/刻度等字段（英文，再映射成中文）。

产物 `cards_db.json`：{ "<internal_id>": [type_en, race_en, attribute_en, level, linkval, scale] }
运行期 rushduel.py 直接按内部 id 取；取不到的卡只靠卡框推断分类，属性/等级缺省。
"""
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

def _find_up(start: Path, name: str) -> Path:
    for p in [start, *start.parents]:
        cand = p / name
        if cand.is_file():
            return cand
    raise FileNotFoundError(name)


def _project_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "src" / "mdstudio").is_dir():
            return p
    return start


HERE = Path(__file__).resolve().parent                 # tools/rushduel
PROJ = _project_root(HERE)                            # MDCutinStudio
RAW = _find_up(PROJ.parent, "_ygo_raw.json")           # MasterDuel/_ygo_raw.json
CN_DB = HERE / "_cn.cdb"                              # koishipro 中文卡库
# MD 内部 id -> 中文名：data/<install_id>/cards_auto.json（install_id 会变，故按名搜）
_cache_dir = PROJ / "data"
_caches = list(_cache_dir.rglob("cards_auto.json")) if _cache_dir.is_dir() else []
CACHE = _caches[0] if _caches else (_cache_dir / "cards_auto.json")
OUT = HERE / "cards_db.json"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("·", ".").replace("・", ".").replace("•", ".")
    s = re.sub(r"\s+", "", s)
    return s.strip()


def core(s: str) -> str:
    s = norm(s)
    # 去掉中文本地化差异最大的功能字与标点，只留“实词”
    s = re.sub(r"[的之・·•\-－．.、，,：:／/\s]", "", s)
    return s


def load_ygo():
    d = json.load(open(RAW, encoding="utf-8"))
    data = d.get("data", d)
    if isinstance(data, dict):
        data = list(data.values())
    by_id = {}
    for c in data:
        by_id[str(c.get("id"))] = c
    return by_id


def load_cn():
    con = sqlite3.connect(str(CN_DB))
    cur = con.cursor()
    cur.execute("SELECT id, name FROM texts")
    rows = cur.fetchall()
    con.close()
    cn = {}
    for cid, name in rows:
        if not name:
            continue
        cn.setdefault(norm(name), cid)        # 精确（归一后）
    return cn


def build_index(cn):
    idx = {}
    for k, v in cn.items():
        idx.setdefault(k[:2], []).append(k)
    return idx


def lookup(name, cn, idx):
    n = norm(name)
    if n in cn:
        return cn[n]
    c = core(name)
    if c in cn:
        return cn[c]
    # 模糊兜底：同前两字桶内找最接近
    for source in (c, n):
        cands = idx.get(source[:2], [])
        if not cands:
            continue
        best, best_r = None, 0.0
        for k in cands:
            # 字符集接近才比（避免长短悬殊误匹配）
            if abs(len(k) - len(source)) > 3:
                continue
            r = _ratio(source, k)
            if r > best_r:
                best_r, best = r, k
        if best and best_r >= 0.82:
            return cn[best]
    return None


def _ratio(a, b):
    # 轻量序列相似度（避免对 14k×14k 全量 difflib）
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def main():
    ygo = load_ygo()
    cn = load_cn()
    idx = build_index(cn)
    md = json.load(open(CACHE, encoding="utf-8"))["names"]

    out = {}
    miss = 0
    for cid, name in md.items():
        pc = lookup(name, cn, idx)
        if pc is None:
            miss += 1
            continue
        c = ygo.get(str(pc))
        if not c:
            miss += 1
            continue
        rec = [
            c.get("type") or "",
            c.get("race") or "",
            c.get("attribute") or "",
            c.get("level") or 0,
            c.get("linkval") or 0,
            c.get("scale") or 0,
        ]
        out[str(cid)] = rec

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"written {len(out)} cards (missed {miss} of {len(md)}) -> {OUT}")


if __name__ == "__main__":
    main()
