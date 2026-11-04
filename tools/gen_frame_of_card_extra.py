"""生成 tools/overframe/frame_of_card_extra.json（卡框类型补丁表）

主表 frame_of_card.json 有三类毛病，这个脚本把它们一次性修好：

1. 灵摆漏判。主表是按另一套卡号编的，有 180 多张灵摆卡被当成了 1（普通效果怪兽），
   结果套了非灵摆的框和插图窗。这里按卡库的 SubType（效果=靈擺 / 通常=靈擺）改回 14 / 13。
2. 完全没条目的卡。用两边都有数据的卡标定 SubType -> 类型码，只取标定唯一的值，
   再对缺条目的卡精确查表（1 = 效果是默认值，不写进来）。
3. 逐个在 ygocdb.com / ygomd.com 上核对过类型的卡。这类原表分不开或标错了，
   例如灵摆仪式和灵摆同调在原表里都是 17，仪式灵魂怪被当成了灵摆。

补丁表会覆盖主表同号条目。**游戏更新后要重跑一次。**

用法：python tools/gen_frame_of_card_extra.py
依赖：超框2 的 card-catalog-v1.json.br（第三方数据，不随包分发），路径见 CATALOG。
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "..", "tools", "overframe")
MAIN = os.path.join(BASE, "frame_of_card.json")
EXTRA = os.path.join(BASE, "frame_of_card_extra.json")

# 第三方卡库（超框2 随包带的那个），只用来推导，不进产物。
# 路径按需给：第一个参数 > 环境变量 MDSTUDIO_CATALOG > 工程旁边的默认位置。
CATALOG = (sys.argv[1] if len(sys.argv) > 1
           else os.environ.get("MDSTUDIO_CATALOG")
           or os.path.join(HERE, "..", "超框2",
                           "MD-Card-Mod-Tool-main", "MdCardModTool",
                           "Resources", "card-catalog-v1.json.br"))
# 卡名表目录：程序自己生成的 cards_*.json 缓存
NAME_ROOT = os.path.join(HERE, "..", "data")

# 逐张核过类型的卡（ygocdb 的 text.types / ygomd 的卡片资料）
MANUAL = {
    "3118": 3, "3190": 3, "3191": 3, "22538": 3,      # 仪式怪兽
    "22185": 0,                                        # 通常（调整）
    "22520": 14, "22521": 14, "22522": 14, "22920": 14, "22925": 14,   # 灵摆效果
    "12772": 3, "12773": 3, "19170": 3, "19171": 3,    # 仪式灵魂，不是灵摆
    "17145": 16, "20595": 16, "20937": 16, "21627": 16, "22134": 16, "22539": 16,  # 灵摆仪式
    "23031": 12,                                       # 衍生物（怪兽/协调/衍生物）
}


def main():
    import brotli

    fm = {int(k): int(v) for k, v in
          json.load(open(MAIN, encoding="utf-8"))["frame_of_card"].items()}
    cat = {e["CardId"]: e for e in
           json.loads(brotli.decompress(open(CATALOG, "rb").read()))}
    names, newest = {}, 0.0
    for root, _, files in os.walk(NAME_ROOT):
        for fn in files:
            if fn.startswith("cards_") and fn.endswith(".json"):
                try:
                    d = json.load(open(os.path.join(root, fn), encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(d, dict) and d.get("names"):
                    if not names or os.path.getmtime(os.path.join(root, fn)) > newest:
                        names, newest = d["names"], os.path.getmtime(os.path.join(root, fn))
    if not names:
        raise SystemExit("找不到卡名缓存（先开一次程序让它生成 cards_*.json）")
    by_name = collections.defaultdict(list)
    for k, v in names.items():
        if v:
            by_name[v].append(int(k))

    out = {}

    # 1) 灵摆漏判
    pend = {"通常=靈擺": 13, "效果=靈擺": 14}
    n1 = 0
    for cid, e in cat.items():
        code = pend.get(str(e.get("SubType") or ""))
        if code is None or fm.get(cid) == code:
            continue
        out[str(cid)] = code
        n1 += 1

    # 2) 缺条目的卡：先标定 SubType -> 类型码
    hexv, strv = collections.defaultdict(set), collections.defaultdict(set)
    for cid, e in cat.items():
        code, st = fm.get(cid), str(e.get("SubType") or "")
        if code is None or not st:
            continue
        (hexv if st.startswith("0x") else strv)[st].add(code)
    cal_h = {k: next(iter(v)) for k, v in hexv.items() if len(v) == 1}
    cal_s = {k: next(iter(v)) for k, v in strv.items() if len(v) == 1}
    n2 = 0
    for k, nm in names.items():
        cid = int(k)
        if cid in fm or any(s != cid and s in fm for s in by_name[nm]):
            continue
        st = str(cat.get(cid, {}).get("SubType") or "")
        code = cal_h.get(st) if st.startswith("0x") else cal_s.get(st)
        if code is None or code == 1:      # 1 是默认值，不用写
            continue
        out[str(cid)] = code
        n2 += 1

    # 3) 核过的
    n3 = 0
    for k, v in MANUAL.items():
        if fm.get(int(k)) != v:
            out[k] = v
            n3 += 1

    data = {
        "_source": (
            "由 tools/gen_frame_of_card_extra.py 生成，覆盖 frame_of_card.json 的三类问题：\n"
            "1) 灵摆漏判：卡库标 效果=靈擺 / 通常=靈擺 的卡，原表当成了 1(效果)，改回 14 / 13。\n"
            "2) 原表完全没条目的卡：标定 SubType -> 类型码后精确查表；1(效果) 是默认值，不写。\n"
            "3) 逐个在 ygocdb.com / ygomd.com 核对过类型的卡（灵摆仪式、仪式灵魂、灵摆效果等）。\n"
            "这里的条目覆盖 frame_of_card.json 的同号条目。游戏更新后需要重新生成。"
        ),
        "frame_of_card": out,
    }
    json.dump(data, open(EXTRA, "w", encoding="utf-8"),
              ensure_ascii=False, indent=0, sort_keys=True)
    print("① 灵摆漏判 %d | ② 缺条目 %d | ③ 人工核对 %d" % (n1, n2, n3))
    print("补丁 %d 条" % len(out))
    print(dict(sorted(collections.Counter(out.values()).items())))


if __name__ == "__main__":
    main()
