"""超框（over-frame）卡图 —— Floowan OfGradient 风格，单模式。

超框 = 卡图主体跨出插图框线的那种卡面。游戏不是靠一个散落的文件认超框，而是靠
一个 AssetBundle 里的 TextAsset：

    of_card_asset   little-endian uint16 对：触发卡号, 底图卡号
    普通卡转超框两个数字相同；两个不同 = 用 A 卡的演出、画 B 的卡

关键点（2026-10-29 游戏更新后实测）：这个 TextAsset **不在 data.unity3d 里认**，
而是被一个 8 位十六进制命名的 bundle 松散覆盖着，位置在本账号的
`LocalData/<账号>/0000/<前两位>/<id>`。bundle id 是资源路径的 crc32，游戏更新会变
（历史：a589d3b5 → 22817d01 → 99f84cce）。所以 id 必须动态定位，不能写死。

只换纹理不改这张表 → 游戏按普通卡渲染，超出的部分直接被裁掉。

素材：
    tools/overframe/solid/card_frameNN.png     游戏原生完整卡面（无彩虹）
    tools/overframe/prismatic/card_frameNN.png 棱彩：按卡型调色板重上色最外 28px
    tools/overframe/border_rainbow.png         虹彩：全彩虹硬边环
    tools/overframe/border_glory.png           幻彩：柔和淡彩环带
    tools/overframe/border_soft.png            官方：软珍珠膜发光

合成：
    整幅画 Cover 填进插图窗 → 卡框压顶 → 外沿特效叠加（虹彩/幻彩/官方）
    → 主体（透明 PNG）放大跨出框线 → 效果框压顶。

边框效果（五种，互斥）：
    prismatic   棱彩：卡框换成按卡型配色的珠光框（超框1 样式），越贴合本卡颜色
    iridescent  虹彩：原生框 + 全彩虹硬边环（of3）
    glory       幻彩：原生框 + 柔和淡彩环带（取色自 OF3 成品卡）
    official    官方：原生框 + 软珍珠膜发光（of1/of2，游戏官方超框的观感）
    keep        不改边框：原生框，只换背景与主体
"""

import json
import struct
from pathlib import Path

from PIL import Image

from mdstudio import runtime
from mdstudio.core.crc import get_crc
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr

CARD_W = 704
CARD_H = 1024

# 棱彩：按卡型调色板重上色最外 28px 的卡框（gen_prismatic.py 生成）
PRISMATIC_DIR = "tools/overframe/prismatic"
# 不带彩虹的完整卡面（Floowan 原素材）。虹彩/幻彩/官方/不改边框都用它打底。
SOLID_DIR = "tools/overframe/solid"
# 游戏原生的框线层，只有标题栏、效果栏和细边，中间大片透明。
# 当独立卡面用会露洞，只在需要"只叠框线"的场合才用得上，目前不参与合成。
NATIVE_DIR = "tools/overframe/cardframes"
FRAME_MAP = "tools/overframe/frame_of_card.json"
# 补丁表：修主表里灵摆漏判、以及完全没条目的卡（来源见文件里的 _source）
FRAME_MAP_EXTRA = "tools/overframe/frame_of_card_extra.json"

GATE_NAME = "of_card_asset"
REC = 4
# 当前版本实测的 gate bundle id（2026-10-29 更新后）。更新再变时会被动态扫描覆盖。
GATE_KNOWN_IDS = ("99f84cce",)
_GATE_CACHE = None  # (root_str, bundle_path)

# 插图窗 / 灵摆插图窗（Floowan 实测值）
ART_WINDOW = (89, 191, 527, 528)
PENDULUM_ART_WINDOW = (50, 186, 604, 451)
# 效果栏上沿：主体最多冲到这里，避免压住文字区
LORE_TOP = 759
PENDULUM_LORE_TOP = 640

# 效果框（含黑边）外接矩形，用于把它单独压在主体之上、透明度可调
# 灵摆的上沿是 637：六个灵摆框（13/14/15/16/17/19）的顶部边框都量到 y=637..643，
# 原来从 639 起裁，把最上面两行切掉了，左右两个灵摆刻度框看着就缺了顶边。
LORE_RECT = (41, 755, 623, 217)
PENDULUM_LORE_RECT = (41, 637, 623, 334)

# 主体相对插图窗的放大倍数（>1 才会跨出框线）
OVERFLOW_SCALE = 1.38
SUBJECT_ALPHA = 12

# 边框效果：互斥单选。名字进 edits.json，所以不要随便改。
EFFECT_PRISMATIC = "prismatic"     # 棱彩：按卡型调色板重上色最外 28px（超框1 样式）
EFFECT_IRIDESCENT = "iridescent"   # 虹彩：全彩虹硬边环（of3）
EFFECT_GLORY = "glory"             # 幻彩：柔和淡彩环带（数据键保持 glory）
EFFECT_OFFICIAL = "official"       # 官方：游戏官方超框那种软珍珠膜发光（of1/of2）
EFFECT_KEEP = "keep"               # 不改边框：只换背景与主体
EFFECTS = (EFFECT_PRISMATIC, EFFECT_IRIDESCENT, EFFECT_GLORY, EFFECT_OFFICIAL, EFFECT_KEEP)

# 老 edits.json 里的键映射到新名字，别让用户之前存的记录失效。
LEGACY_EFFECTS = {
    "gradient": EFFECT_PRISMATIC,
    "holo": EFFECT_IRIDESCENT,
    "rainbow": EFFECT_IRIDESCENT,
    "of_native": EFFECT_OFFICIAL,
    "native": EFFECT_KEEP,
}

def normalize_effect(effect) -> str:
    """把效果名规范成当前五选一；老键名映射过来，认不出就用棱彩。"""
    e = (effect or "").strip()
    e = LEGACY_EFFECTS.get(e, e)
    return e if e in EFFECTS else EFFECT_PRISMATIC


# 外沿特效叠加素材：不随卡型变化，整张卡通用，合成时盖在原生卡框之上。
BORDER_GLORY_FILE = "tools/overframe/border_glory.png"
BORDER_RAINBOW_FILE = "tools/overframe/border_rainbow.png"
# 官方：从 OF4 里 10 张官方超框卡截图取中位数提取的实拍边框带（gen_official.py）
BORDER_OFFICIAL_FILE = "tools/overframe/border_official.png"

# 哪些效果需要在合成时叠加外沿素材（棱彩已把颜色画进卡框，不需要叠）
BORDER_OVERLAY = {
    EFFECT_IRIDESCENT: BORDER_RAINBOW_FILE,
    EFFECT_GLORY: BORDER_GLORY_FILE,
    EFFECT_OFFICIAL: BORDER_OFFICIAL_FILE,
}

FRAME_NAMES = {
    0: "通常怪兽", 1: "效果怪兽", 2: "仪式怪兽", 3: "融合怪兽",
    7: "魔法卡", 8: "陷阱卡", 9: "衍生物", 10: "同调怪兽", 12: "超量怪兽",
    13: "灵摆通常", 14: "灵摆效果", 15: "灵摆超量", 16: "灵摆同调",
    17: "灵摆融合", 18: "连接怪兽", 19: "灵摆仪式",
}

# 连接怪兽的 8 个箭头。做法照 Floowan 超框1 的 LinkArrowOverlay：**不抠掩码**，
# 而是用近黑芯拟合出一个几何三角形，再用有符号距离场（SDF）画银边和填充。
#
# 为什么不抠：箭头由「深藏青三角 + 银灰描边」组成，银边的颜色和卡框自带的银线
# 几乎一样，一放宽阈值就顺着银线蔓延到整张卡；收紧又漏掉半圈描边。拟合三角形后
# 描边是算出来的，边界由几何决定，天然干净。
#
# 取样框比字形大一圈，给银边和投影留余量。数值与超框1 一致。
LINK_FRAME = 18
LINK_ARROWS = (
    (42, 142, 120, 125),   # 左上
    (282, 132, 140, 72),   # 上
    (537, 141, 125, 125),  # 右上
    (30, 382, 72, 145),    # 左
    (598, 382, 74, 145),   # 右
    (26, 642, 135, 132),   # 左下
    (282, 700, 140, 74),   # 下
    (536, 642, 135, 132),  # 右下
)
# 近黑芯的亮度上限。只用来「定位」字形，银边的中间调不参与拟合。
# 实测芯是 (0,8,38) 亮度约 9，银边亮度 105~125，卡框蓝底 65~90。
LINK_CORE_LUM = 32
# 下面这组几何与配色照抄 超框1 的 LinkArrowOverlay.cs：它是在真机上对着游戏
# 渲染效果调出来的。早前我自己从卡框贴图上量的那套（银边 6px、色值 97~143）
# 偏细偏暗，画在花哨主体上只剩一个深色块。
LINK_RIM_PX = 6.4        # 银白金属边外半径
                         # 常压在浅色主体上，太宽会糊成一片白）
# 官方卡上银边是直接接深红填充的，中间并没有黑线——那一小段深色是填充自己的
# 边缘色。之前按「黑缝」画了一条，放大看就是多余的轮廓线。
LINK_INSET_PX = 0.0
LINK_HALO_PX = 3.4       # 深色描边 + 柔投影的总宽度
LINK_HALO_SOLID = 1.3    # 紧贴银边的实心深色部分。光靠柔投影托不住浅色背景，
                         # 箭头会和白色主体糊在一起，看着像描了一圈白边
LINK_EDGE_AA = 0.85      # 边缘抗锯齿半宽
LINK_EXPAND_PX = -0.6    # 填充区比原三角再收一点，让银边显出来
LINK_INSET = (8, 8, 12)           # 黑缝（宽度为 0 时不用）
# 金属边内侧要偏红偏暗（官方实测 145,81,81）：之前用灰蓝 (150,162,182)，
# 紧贴橙红填充时被抗锯齿混成一条浅色，看着像描了一圈白边。
LINK_RIM_INNER = (178, 156, 152)
LINK_RIM_OUTER = (218, 230, 244)  # 金属边外沿的高光（不取纯白，官方峰值 211,230,247）
LINK_HALO_ALPHA = 215    # 深色描边的峰值不透明度
# 点亮色：中心白热 → 中段橙红 → 边缘深红。只做两段（中心橙、边缘红）时，
# 中心不够亮、整体偏黄，看起来像一块扁平色块，没有「烧起来」的感觉。
LINK_LIT_HOT = (255, 242, 224)    # 内切中心的白热
LINK_LIT_MID = (248, 126, 58)     # 中段橙红
LINK_LIT_EDGE = (146, 18, 12)     # 边缘深红
# 未点亮方向不重画，直接从卡框上原样搬运（见 _link_blit_arrow），所以不需要配色。

# frame_of_card.json 里存的是卡的类型码，游戏原卡框 PNG 是另一套编号。
# 实测只有 0/1/9/18 与灵摆组恰好重合，其余都要翻译：
#   类型码 2=融合 -> PNG 03；3=仪式 -> 02；7=同调 -> 10；8=超量 -> 12；
#   10=陷阱 -> 08；9=魔法 -> 07；12=衍生物 -> 09
# 灵摆这四个是错位的（用卡名和框色双向核对过）：
#   15=灵摆融合 -> 紫框 17；16=灵摆仪式 -> 蓝框 19；
#   17=灵摆同调 -> 白框 16；19=灵摆超量 -> 黑框 15
# PNG 编号就是游戏 data.unity3d 里 card_frameNN 的 NN，文件与资产一一对应。
CODE_TO_PNG = {
    0: 0, 1: 1, 2: 3, 3: 2, 7: 10, 8: 12, 9: 7, 10: 8, 12: 9,
    18: 18, 13: 13, 14: 14,
    15: 17, 16: 19, 17: 16, 19: 15,
}
# 灵摆卡下半张是灵摆效果栏，插图区只在上半，几何完全不同
PENDULUM = {13, 14, 15, 16, 17, 19}

# 注：0 通常 / 1 效果 已由卡 22117 实测对上；9 魔法 2789 张、10 陷阱 2016 张佐证
_map = None


# ------------------------------------------------------------------ 素材定位
_aliases = None


def _frame_aliases() -> dict:
    """类型表没覆盖到的卡号，用同名兄弟的类型码顶上。

    类型表是按另一套卡号编的，22xxx 这批真卡号里有 142 个没有条目，而它们的
    同名占位号有（例如 #22519 缺，它的占位号 #3182 是 14 = 灵摆效果）。
    只在查不到时才读一次名字缓存，查到就缓存住。
    """
    global _aliases
    if _aliases is not None:
        return _aliases
    names = None
    try:
        cands = sorted(runtime.data_dir().glob("*/cards_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        cands = []
    for p in cands:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("names"):
            names = d["names"]
            break
    _aliases = {}
    if not names:
        return _aliases
    by_name = {}
    for cid, nm in names.items():
        if nm:
            by_name.setdefault(nm, []).append(cid)
    for cid, nm in names.items():
        if not nm or cid in _map:
            continue
        for sib in by_name[nm]:
            if sib != cid and sib in _map:
                _aliases[cid] = _map[sib]
                break
    return _aliases


def frame_of_card(card_id: int) -> int:
    """这张卡用哪套卡框（返回游戏原卡框 PNG 的编号）。查不到给 1（效果怪兽）。"""
    global _map
    if _map is None:
        f = runtime.bundle_root() / FRAME_MAP
        try:
            _map = json.loads(f.read_text(encoding="utf-8")).get("frame_of_card") or {}
        except (OSError, ValueError):
            _map = {}
        # 补丁表覆盖主表：灵摆漏判和缺条目的卡都在这里改正
        extra = runtime.bundle_root() / FRAME_MAP_EXTRA
        try:
            _map.update(json.loads(extra.read_text(encoding="utf-8")).get("frame_of_card") or {})
        except (OSError, ValueError):
            pass
    key = str(int(card_id))
    raw = _map.get(key)
    if raw is None:
        raw = _frame_aliases().get(key)
    if raw is None:
        raw = 1
    return CODE_TO_PNG.get(int(raw), 1)


def frame_name(n: int) -> str:
    return FRAME_NAMES.get(int(n), tr("Unknown"))


def frames() -> list:
    """随包带了哪些卡框（原生整框 PNG），按编号排"""
    root = runtime.bundle_root() / NATIVE_DIR
    out = []
    for f in sorted(root.glob("card_frame*.png")):
        stem = f.stem.replace("card_frame", "")
        try:
            out.append(int(stem))
        except ValueError:
            continue
    return out


def frame_path(n: int, effect: str = EFFECT_PRISMATIC) -> Path:
    """按边框效果取卡框图。

    棱彩用按卡型配色的 prismatic/ 素材；虹彩/幻彩/官方/不改边框都用原生完整卡面
    （外沿特效在合成时另行叠加）。prismatic 缺这个卡型时退回原生卡面。
    """
    nn = int(n)
    if effect == EFFECT_PRISMATIC:
        pris = runtime.bundle_root() / PRISMATIC_DIR / f"card_frame{nn:02d}.png"
        if pris.is_file():
            return pris
    solid = runtime.bundle_root() / SOLID_DIR / f"card_frame{nn:02d}.png"
    if solid.is_file():
        return solid
    # 兜底：游戏原生框线层
    native = runtime.bundle_root() / NATIVE_DIR / f"card_frame{nn:02d}.png"
    if native.is_file():
        return native
    raise MDStudioError(tr("frame not found: %s") % native)


def frame_image(n: int, effect: str = EFFECT_PRISMATIC) -> Image.Image:
    return Image.open(frame_path(n, effect)).convert("RGBA")


def _link_core_mask(part: Image.Image) -> list:
    """在取样框里找「近黑芯」的像素，返回 [(x, y), ...]。

    只认极暗的填充部分（亮度 <= LINK_CORE_LUM），银边的中间调不要，
    所以拟合出来的三角形边正好落在「填充与黑缝」之间。
    必须要求不透明：取样框会伸进插图窗，窗内是透明像素，转 RGB 后是 (0,0,0)，
    不加这个门槛会被当成一大片黑芯，把三角形拟合到窗框上去。
    """
    px = part.load()
    w, h = part.size
    out = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a >= 200 and r + g + b <= LINK_CORE_LUM * 3:
                out.append((x, y))
    return out


def _link_largest_blob(points: list, w: int, h: int) -> list:
    """4 邻接找最大连通块。箭头旁边常有别的暗像素（黑缝、插图窗边），要挑最大那块。"""
    if not points:
        return []
    pts = set(points)
    seen = set()
    best = []
    for start in points:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        blob = []
        while stack:
            x, y = stack.pop()
            blob.append((x, y))
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w and 0 <= ny < h and (nx, ny) in pts and (nx, ny) not in seen:
                    seen.add((nx, ny))
                    stack.append((nx, ny))
        if len(blob) > len(best):
            best = blob
    return best


def _link_convex_hull(points: list) -> list:
    """Andrew 单调链凸包。"""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _link_fit_triangle(blob: list, x0: int, y0: int, art_cx: float, art_cy: float):
    """把锯齿状的近黑字形拟合成一个三角形，返回 (p0, p1, p2, cx, cy)。

    取凸包上能围出最大面积的三点，不预设哪个是尖端。早前是「离插图窗中心最远
    的点当尖端 + 底边取最大面积两点」，对角箭头的两个直角顶点到中心距离相近，
    容易选错尖端，整个三角形就跟着转，画出来和原字形对不上（左上那个还冒了尖刺）。
    对三角形凸包而言，最大面积三点就是它自己的三个角，绕开了这个歧义。

    x0/y0/art_cx/art_cy 保留是为了兼容调用处，现在不参与选点。
    """
    if len(blob) < 8:
        return None
    hull = _link_convex_hull(blob)
    if len(hull) < 3:
        return None

    best = -1.0
    tri = (hull[0], hull[0], hull[0])
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        for j in range(i + 1, n):
            bx, by = hull[j]
            for k in range(j + 1, n):
                cx_, cy_ = hull[k]
                area = abs((bx - ax) * (cy_ - ay) - (cx_ - ax) * (by - ay))
                if area > best:
                    best = area
                    tri = (hull[i], hull[j], hull[k])
    if best < 4:
        return None

    cx = sum(p[0] for p in blob) / len(blob)
    cy = sum(p[1] for p in blob) / len(blob)

    def _c(p):
        return (p[0] + 0.5, p[1] + 0.5)

    return _c(tri[0]), _c(tri[1]), _c(tri[2]), cx, cy


def _link_grow(tri, e: float):
    """把三角形的三条边各向外平移 e 像素，返回新顶点。

    用内心（按对边长加权的重心）等比放大，而不是沿质心方向推顶点——后者对
    不等边的三角形会让三条边外扩量不一致，盖不住原字形留下的那一圈。
    """
    a, b, c = tri
    la = ((b[0] - c[0]) ** 2 + (b[1] - c[1]) ** 2) ** 0.5
    lb = ((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2) ** 0.5
    lc = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    per = la + lb + lc
    if per <= 1e-6:
        return tri
    area = 0.5 * abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))
    r_in = 2.0 * area / per
    if r_in <= 1e-3:
        return tri
    icx = (a[0] * la + b[0] * lb + c[0] * lc) / per
    icy = (a[1] * la + b[1] * lb + c[1] * lc) / per
    s = (r_in + e) / r_in
    return tuple((icx + (p[0] - icx) * s, icy + (p[1] - icy) * s) for p in tri)


def _link_sd_triangle(px, py, a, b, c) -> float:
    """到三角形的有符号距离，内部为负。改编自 Inigo Quilez 的写法。"""

    def edge_dist(ex, ey, vx, vy):
        ee = ex * ex + ey * ey
        t = max(0.0, min(1.0, (vx * ex + vy * ey) / ee)) if ee > 1e-8 else 0.0
        qx, qy = vx - ex * t, vy - ey * t
        return qx * qx + qy * qy

    e0 = (b[0] - a[0], b[1] - a[1])
    e1 = (c[0] - b[0], c[1] - b[1])
    e2 = (a[0] - c[0], a[1] - c[1])
    v0 = (px - a[0], py - a[1])
    v1 = (px - b[0], py - b[1])
    v2 = (px - c[0], py - c[1])

    d = min(edge_dist(e0[0], e0[1], v0[0], v0[1]),
            edge_dist(e1[0], e1[1], v1[0], v1[1]),
            edge_dist(e2[0], e2[1], v2[0], v2[1]))

    s = 1.0 if (e0[0] * e2[1] - e0[1] * e2[0]) >= 0 else -1.0
    s0 = s * (v0[0] * e0[1] - v0[1] * e0[0])
    s1 = s * (v1[0] * e1[1] - v1[1] * e1[0])
    s2 = s * (v2[0] * e2[1] - v2[1] * e2[0])
    return -(d ** 0.5) * (1.0 if min(s0, s1, s2) >= 0 else -1.0)


def _link_band(d, inner, outer, aa) -> float:
    """环带 inner <= d < outer 的覆盖率，两端各留 aa 的过渡。"""
    def inside(edge):
        if edge + aa <= d:
            return 0.0
        if edge - aa >= d:
            return 1.0
        t = (d - (edge + aa)) / ((edge - aa) - (edge + aa))
        return t * t * (3 - 2 * t)

    return max(0.0, min(1.0, inside(outer) - inside(inner)))


def _link_blit_arrow(part: Image.Image, seed) -> Image.Image:
    """把未点亮的箭头从卡框上**原样搬运**出来，一个像素都不改。

    做法是从近黑芯出发做连通扩张，只吃「低饱和」的邻像素——箭头芯和银边的
    饱和度都在 60 以下，卡框蓝底在 130 以上，两者分得很开。因为不套几何遮罩，
    搬出来的形状 100% 是原字形：既不会在尖角处多出一根按法向外扩的细刺，也
    不会把箭头旁边那条插图窗灰框顺手带进来（中间隔着一圈蓝底，不连通）。

    扩张限 10 层（银边最宽处约 6px），防止顺着别处的银色装饰一直爬。
    """
    from collections import deque

    w, h = part.size
    src = part.load()
    mask = bytearray(w * h)
    q = deque()
    for sx, sy in seed:
        if 0 <= sx < w and 0 <= sy < h and not mask[sy * w + sx]:
            mask[sy * w + sx] = 1
            q.append((sx, sy))
    if not q:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    sat_max = 70.0
    for _ in range(10):
        if not q:
            break
        for _ in range(len(q)):
            x, y = q.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if mask[ny * w + nx]:
                    continue
                r, g, b, a = src[nx, ny]
                if a < 20:
                    continue
                if max(r, g, b) - min(r, g, b) >= sat_max:
                    continue          # 蓝底，到此为止
                mask[ny * w + nx] = 1
                q.append((nx, ny))

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dst = out.load()
    for y in range(h):
        for x in range(w):
            if mask[y * w + x]:
                dst[x, y] = src[x, y]
    # 遮罩是二值的，边缘一刀切下来是硬锯齿；原字形本身带抗锯齿，被这一刀切掉
    # 之后，未点亮箭头一旦跟着效果框变半透明，斜边上就会露出台阶。
    # 只模糊 alpha（颜色另做外扩），否则 RGBA 一起模糊会让边缘和透明黑混合发暗。
    from PIL import ImageFilter
    alpha = out.getchannel("A").filter(ImageFilter.GaussianBlur(0.9))
    # 颜色层：把透明区的颜色用邻近像素填上，避免边缘出现半透明黑边
    rgb = out.convert("RGB").filter(ImageFilter.MaxFilter(3))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def _link_paint_arrow(part: Image.Image, tri, cx: float, cy: float) -> Image.Image:
    """按 SDF 画一支「点亮」的箭头：投影 → 银白边 → 黑缝 → 橙红渐变。

    只输出箭头本身（其余像素全透明），这样贴回画布时不会把取样框内的卡框像素
    一起带上去。卡框上只有深色箭头，橙红版没有现成的，所以这层只能重画。
    """
    tip, ba, bb = tri
    w, h = part.size
    rim_in, rim_out = LINK_RIM_INNER, LINK_RIM_OUTER
    inset_col = LINK_INSET
    halo_alpha = LINK_HALO_ALPHA

    def _lit_color(t: float):
        """t=0 中心白热，t=0.45 转橙红，t=1 边缘深红。"""
        if t < 0.45:
            u = t / 0.45
            a_, b_ = LINK_LIT_HOT, LINK_LIT_MID
        else:
            u = (t - 0.45) / 0.55
            a_, b_ = LINK_LIT_MID, LINK_LIT_EDGE
        return tuple(a_[i] + (b_[i] - a_[i]) * u for i in range(3))

    # 距离场用「三条边各外移 rim 之后的三角」而不是原三角做偏移：SDF 的
    # offset 在锐角处按法向发散，45° 角会拉出一根 14px 长的细刺；拿圆角去压
    # 又必然把尖角磨钝（就是「尖部没盖住」的样子）。换成外移后的三角，尖角
    # 保持原本的角度，整支箭头的轮廓就是它本身。
    outer = _link_grow((tip, ba, bb), LINK_EXPAND_PX + LINK_RIM_PX)
    fill_line = -LINK_RIM_PX - LINK_INSET_PX      # 填充边界在外移三角的 d 值

    # 内切圆半径：填充区 SDF 深度的上限，用来把「边上深、中心亮」的渐变归一化
    _e1 = ((ba[0] - tip[0]) ** 2 + (ba[1] - tip[1]) ** 2) ** 0.5
    _e2 = ((bb[0] - ba[0]) ** 2 + (bb[1] - ba[1]) ** 2) ** 0.5
    _e3 = ((tip[0] - bb[0]) ** 2 + (tip[1] - bb[1]) ** 2) ** 0.5
    _peri = _e1 + _e2 + _e3
    _area = 0.5 * abs((ba[0] - tip[0]) * (bb[1] - tip[1])
                      - (bb[0] - tip[0]) * (ba[1] - tip[1]))
    r_in = ((2.0 * _area / _peri) if _peri > 0 else 1.0) - LINK_INSET_PX
    if r_in < 1.0:
        r_in = 1.0

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    buf = out.load()

    for ly in range(h):
        for lx in range(w):
            px, py = lx + 0.5, ly + 0.5
            d = _link_sd_triangle(px, py, *outer)
            if d >= LINK_HALO_PX + LINK_EDGE_AA:
                continue

            ur = ug = ub = ua = 0

            def over(r, g, b, a):
                nonlocal ur, ug, ub, ua
                if a <= 0:
                    return
                oa = a / 255.0
                uu = ua / 255.0
                na = oa + uu * (1 - oa)
                if na <= 0:
                    ur = ug = ub = ua = 0
                    return
                ur = int(max(0, min(255, round((r * oa + ur * uu * (1 - oa)) / na))))
                ug = int(max(0, min(255, round((g * oa + ug * uu * (1 - oa)) / na))))
                ub = int(max(0, min(255, round((b * oa + ub * uu * (1 - oa)) / na))))
                ua = int(max(0, min(255, round(na * 255))))

            # 1) 紧贴金属边的实心深色描边 + 往外渐隐的柔边。实心那一段是关键：
            #    箭头常常压在白色主体上，只有柔投影的话边缘会和背景糊成一片。
            solid = _link_band(d, 0.0, LINK_HALO_SOLID, LINK_EDGE_AA)
            if solid > 0.004 and halo_alpha > 0:
                over(0, 0, 0, int(round(halo_alpha * solid)))
            sc = _link_band(d, LINK_HALO_SOLID, LINK_HALO_PX, LINK_EDGE_AA)
            if sc > 0.004 and halo_alpha > 0:
                t = max(0.0, min(1.0, (d - LINK_HALO_SOLID)
                                  / (LINK_HALO_PX - LINK_HALO_SOLID)))
                falloff = (1 - t) * (1 - 0.35 * t)
                a = int(round(halo_alpha * 0.55 * falloff * sc))
                if a > 0:
                    over(0, 0, 0, a)

            # 2)~4) 金属边 / 黑缝 / 填充。
            # 关键：这三段全部画成**不透明**，只在颜色上过渡。抗锯齿做成半透明
            # 的话，箭头的内部分界线叠在白色主体上会被冲淡，看着就是描了一圈白边。
            # 只有最外圈的深色描边（上面第 1 段）才留半透明，那里是贴背景的。
            if d <= 0.0:
                if d <= fill_line - LINK_EDGE_AA:
                    # 纯填充区
                    fd = d - fill_line
                    t = max(0.0, min(1.0, 1.0 - (-fd) / r_in)) ** 1.5
                    col = _lit_color(t)
                elif d >= -LINK_RIM_PX + LINK_EDGE_AA:
                    # 纯金属边区
                    ot = max(0.0, min(1.0, (d + LINK_RIM_PX) / LINK_RIM_PX))
                    col = tuple(
                        int(round(rim_in[i] + (rim_out[i] - rim_in[i]) * ot))
                        for i in range(3)
                    )
                else:
                    # 交界处：颜色在填充和金属边之间渐变，透明度保持 255
                    span = (-LINK_RIM_PX + LINK_EDGE_AA) - (fill_line - LINK_EDGE_AA)
                    u = ((d - (fill_line - LINK_EDGE_AA)) / span) if span > 1e-6 else 0.0
                    u = max(0.0, min(1.0, u))
                    u = u * u * (3 - 2 * u)
                    fd = d - fill_line
                    t = max(0.0, min(1.0, 1.0 - (-fd) / r_in)) ** 1.5
                    fill_col = _lit_color(t)
                    ot = max(0.0, min(1.0, (d + LINK_RIM_PX) / LINK_RIM_PX))
                    rim_col = tuple(rim_in[i] + (rim_out[i] - rim_in[i]) * ot
                                    for i in range(3))
                    col = tuple(fill_col[i] + (rim_col[i] - fill_col[i]) * u
                                for i in range(3))
                # 最外那 1px 留抗锯齿——它贴的是背景，半透明才对。内部那道
                # 银边/填充的分界必须保持不透明，否则叠在白主体上会发白。
                a_out = 255
                if d > -LINK_EDGE_AA:
                    a_out = int(round(255 * (LINK_EDGE_AA - d) / (2 * LINK_EDGE_AA)))
                    a_out = max(0, min(255, a_out))
                over(int(round(col[0])), int(round(col[1])), int(round(col[2])), a_out)

            if ua > 0:
                buf[lx, ly] = (ur, ug, ub, ua)

    return out


def link_arrow_layers(frame: Image.Image, markers: int = 0):
    """把连接怪兽的八个箭头做成两层，好压在主体之上。

    返回 ``(lit, dim)`` 两张 704×1024 的透明层：

    - ``lit``：这一卡实际拥有的方向。卡框上只有深色箭头，橙红版只能按 SDF
      重画（银白边 + 橙红渐变，官方卡上那个样子）。
    - ``dim``：其余方向。从卡框上原样搬运像素，形状 100% 是原字形。

    分成两层是为了透明度各管各的：未点亮那层可以整体调淡让主体透出来，不影响
    点亮方向。

    markers 是方向掩码（bit0..7 = LINK_ARROWS 顺序）。为 0 时两层都是空的。
    """
    lit = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    dim = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    if not markers:
        return lit, dim

    ax, ay, aw, ah = ART_WINDOW
    art_cx, art_cy = ax + aw / 2.0, ay + ah / 2.0

    for i, (x, y, w, h) in enumerate(LINK_ARROWS):
        x1, y1 = min(x + w, CARD_W), min(y + h, CARD_H)
        x, y = max(0, x), max(0, y)
        part = frame.crop((x, y, x1, y1))
        pw, ph = part.size
        if pw <= 0 or ph <= 0:
            continue

        blob = _link_largest_blob(_link_core_mask(part), pw, ph)
        if not blob:
            continue

        if markers & (1 << i):
            fit = _link_fit_triangle(blob, x, y, art_cx, art_cy)
            if not fit:
                continue
            tip, ba, bb, cx, cy = fit
            lit.alpha_composite(
                _link_paint_arrow(part, (tip, ba, bb), cx, cy), (x, y))
        else:
            # 未点亮的一律原样搬运，重画必然在尖角处露出偏差
            dim.alpha_composite(_link_blit_arrow(part, blob), (x, y))

    return lit, dim


def link_arrow_layer(frame: Image.Image, markers: int = 0,
                     dim_alpha: float = 1.0) -> Image.Image:
    """把 ``link_arrow_layers`` 的两层合成为一张，未点亮那层按 dim_alpha 淡出。

    写进游戏贴图时用这个（游戏只认一张整卡 PNG，没有分层的概念）。
    """
    lit, dim = link_arrow_layers(frame, markers)
    if dim_alpha < 0.999:
        a = max(0.0, min(1.0, dim_alpha))
        dim = dim.copy()
        alpha = dim.getchannel("A").point(lambda v: int(round(v * a)))
        dim.putalpha(alpha)
    out = lit
    out.alpha_composite(dim)
    return out


_marker_cache: dict = {}


def markers_of_card(card_id: int) -> int:
    """这张连接怪兽实际拥有的方向（bit0..7 = LINK_ARROWS 的顺序）。

    方向不在贴图上，而在游戏 CARD_Prop 里：解密后从偏移 8 起每条 8 字节，
    卡号 2 字节、类型 2 字节，剩下 4 字节是小端 uint32，其中 bit0..8 是 ATK/10，
    bit9..17 是 DEF 槽——连接怪兽没有防御力，这一槽存的就是方向掩码。
    （社区整理的 CARD_Link 是关联卡号表，不含方向位。）

    读不到、或这张不是连接怪兽，都返回 0（不出箭头），不影响出图。
    """
    cid = int(card_id)
    if cid in _marker_cache:
        return _marker_cache[cid]
    mask = 0
    found = False       # 找到了这张卡的属性记录才算解析成功; 没找到/没配置成功都不缓存
    try:
        from mdstudio.core import cards as C
        from mdstudio.core.game import Game
        from mdstudio import settings as _st

        cfg = _st.load() or {}
        root = cfg.get("game_dir") or None
        acct = cfg.get("account") or ""
        if acct and root and not _st.valid_account(root, acct):
            acct = ""
        folder, lang = _card_data_dir()
        # 坑: 以前这里要求设置里必须存了游戏目录与账号, 否则整段跳过 = 不画点亮箭头.
        # 但设置文件可能根本没写过(第一次用、或便携目录换名后 config 没跟过来),
        # 软件其它地方靠 Game.detect 自动找也照常能用. 所以这里同样回退到自动探测.
        if folder and lang:
            g = Game.detect(root, group=acct or None)

            indx = C._textasset(g.find_logical(
                f"Card/Data/{folder}/{lang}/CARD_Indx").read_bytes())
            prop = C._textasset(g.find_logical(
                f"Card/Data/{folder}/{lang}/CARD_Prop").read_bytes())
            dec = C.text_decrypt(prop, C.find_key(indx))
            for i in range(8, len(dec) - 7, 8):
                if (dec[i] | (dec[i + 1] << 8)) != cid:
                    continue
                found = True
                tb = dec[i + 2]
                packed = (dec[i + 4] | (dec[i + 5] << 8)
                          | (dec[i + 6] << 16) | (dec[i + 7] << 24))
                # 只有连接怪兽的 DEF 槽才存方向掩码，别的卡型那一槽是别的位域
                if (tb & 0x20) and (tb & 0x0F) in (0x0A, 0x0B):
                    mask = (packed >> 9) & 0xFF
                break
    except Exception:
        mask = 0
        found = False
    if found:
        # 只有解析到记录才缓存. 配置缺失时留空, 补好设置后不用重启也能出箭头
        _marker_cache[cid] = mask
    return mask


def _card_data_dir():
    """CARD_* 所在的 <16hex> 目录与语言，从卡名缓存里复用，省得再扫一遍 bundle。"""
    try:
        cands = sorted(runtime.data_dir().glob("*/cards_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None, None
    for p in cands:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("folder") and d.get("lang"):
            return d["folder"], d["lang"]
    return None, None


_link_split_cache: dict = {}


def link_arrow_split(n: int, effect: str = EFFECT_PRISMATIC, markers: int = 0):
    """带缓存的 (lit, dim) 两层。

    预览与写入都要分层：未点亮那层铺在主体之下（主体盖住它），点亮那层压在主体之上。
    key 里带 effect：棱彩会换掉卡框配色, 箭头层是从卡框搬的像素, 不区分就会串味.
    """
    nn = int(n)
    key = (nn, str(effect), int(markers or 0))
    if key not in _link_split_cache:
        _link_split_cache[key] = link_arrow_layers(frame_image(nn, effect), markers)
    return _link_split_cache[key]


def link_arrow_split_png(n: int, out_dir, effect: str = EFFECT_PRISMATIC,
                         markers: int = 0):
    """把两层各写一张 PNG，返回 (lit_path, dim_path)。供预览分层加载。"""
    lit, dim = link_arrow_split(n, effect, markers)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    nn = int(n)
    lp = d / f"link_lit_{nn}.png"
    dp = d / f"link_dim_{nn}.png"
    lit.save(lp)
    dim.save(dp)
    return lp, dp


_border_cache: dict = {}


def border_overlay(kind: str) -> Image.Image:
    """外沿特效叠加素材（虹彩 / 幻彩 / 官方），整张卡通用，缓存。"""
    if kind in _border_cache:
        return _border_cache[kind]
    rel = BORDER_OVERLAY.get(kind)
    if not rel:
        return Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    im = Image.open(runtime.bundle_root() / rel).convert("RGBA")
    _border_cache[kind] = im
    return im


def art_box(n: int) -> tuple:
    """插图区 (x, y, w, h)。灵摆卡在上半，普通卡在中间开窗。"""
    if int(n) in PENDULUM:
        return PENDULUM_ART_WINDOW
    return ART_WINDOW


def lore_top(n: int) -> int:
    return PENDULUM_LORE_TOP if int(n) in PENDULUM else LORE_TOP


def lore_rect(n: int) -> tuple:
    """效果框（含边框）外接矩形 (x, y, w, h)。"""
    return PENDULUM_LORE_RECT if int(n) in PENDULUM else LORE_RECT


def lore_bottom(n: int) -> int:
    x, y, w, h = lore_rect(n)
    return y + h


# ------------------------------------------------------------------ 卡图路径
def illust_logical(card_id: int) -> str:
    return f"Card/Images/Illust/tcg/{int(card_id)}"


def read_illust(ctx, card_id: int):
    """读游戏里这张卡的当前卡图。返回 (Image, 信息 dict)，找不到给 (None, 原因)

    宽 ≥ 704 说明它已经是超框图（普通卡是 512×512，灵摆是 512×1024）。
    """
    from mdstudio.core.bundle import textures

    p = ctx.game.find_logical(illust_logical(card_id))
    if not p:
        return None, {"error": tr("card %s has no artwork on disk") % card_id}
    try:
        ts = textures(p.read_bytes())
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}
    if not ts:
        return None, {"error": tr("no texture in %s") % illust_logical(card_id)}
    d = ts[0]
    return d.image.convert("RGBA"), {
        "path": str(p),
        "width": int(d.m_Width),
        "height": int(d.m_Height),
        "overframe": int(d.m_Width) >= CARD_W,
    }


def base_art(ctx, card_id: int) -> Path:
    """没选整幅画时的兜底背景：取游戏当前插图，若已是超框图就裁出插图窗。"""
    img, meta = read_illust(ctx, card_id)
    if img is None:
        raise MDStudioError(meta.get("error") or tr("card %s has no artwork") % card_id)
    nn = frame_of_card(card_id)
    if img.size == (CARD_W, CARD_H):
        ax, ay, aw, ah = art_box(nn)
        img = img.crop((ax, ay, ax + aw, ay + ah))
    out = runtime.data_dir() / "overframe" / f"{int(card_id)}_base.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# ------------------------------------------------------------------ 编辑记录
# 应用一次就把用过的素材（复制一份存好）与滑块参数记下来，下次选到这张卡自动加载，
# 否则程序会拿"已经合成好的 704 纹理"当底再合成，越改越乱。
def _edits_file() -> Path:
    return runtime.data_dir() / "overframe" / "edits.json"


def _edits_dir() -> Path:
    d = runtime.data_dir() / "overframe" / "edits"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_edits() -> dict:
    try:
        return json.loads(_edits_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_edit(card_id: int):
    return _load_edits().get(str(int(card_id)))


def _copy_in(src, card_id: int, suffix: str) -> str:
    """把素材复制进 edits 目录，避免依赖外部原文件、也不会被 base_art 覆盖。"""
    if not src:
        return ""
    p = Path(src)
    if not p.is_file():
        return ""
    ext = p.suffix.lower() or ".png"
    dst = _edits_dir() / f"{int(card_id)}_{suffix}{ext}"
    try:
        if p.resolve() == dst.resolve():
            return str(dst)
        dst.write_bytes(p.read_bytes())
        return str(dst)
    except OSError:
        return str(p)


def save_edit(ctx, card_id: int, art, art_name, subject, subject_name,
              scale, dx, dy, lore_alpha=1.0,
              effect=EFFECT_PRISMATIC, holo_scope=None):
    data = _load_edits()
    entry = {}
    art_copy = _copy_in(art, card_id, "art")
    if art_copy:
        entry["art"] = art_copy
        entry["art_name"] = art_name or Path(art_copy).name
    sub_copy = _copy_in(subject, card_id, "sub")
    if sub_copy:
        entry["subject"] = sub_copy
        entry["subject_name"] = subject_name or Path(sub_copy).name
    entry["scale"] = float(scale or 1.0)
    entry["dx"] = int(dx or 0)
    entry["dy"] = int(dy or 0)
    entry["lore_alpha"] = float(lore_alpha if lore_alpha is not None else 1.0)
    entry["effect"] = normalize_effect(effect)
    data[str(int(card_id))] = entry
    try:
        f = _edits_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def clear_edit(ctx, card_id: int):
    data = _load_edits()
    if str(int(card_id)) in data:
        data.pop(str(int(card_id)), None)
        try:
            _edits_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


# ------------------------------------------------------------------ 登记表（开关）
def _gate_entries_at(path: Path):
    """这个 bundle 里的登记条目；不是 gate bundle 就返回 None。

    从字节加载而不是 UnityPy.load(路径)：后者会一直握着文件句柄，
    随后的 os.replace 会被 Windows 判成"文件被占用"（WinError 5）。
    """
    import UnityPy

    try:
        env = UnityPy.load(path.read_bytes())
    except Exception:
        return None
    o = _gate_object(env)
    if o is None:
        return None
    try:
        return _parse_script(_script_bytes(o.read()))
    except Exception:
        return None


def _bundle_has_gate(path: Path) -> bool:
    return _gate_entries_at(path) is not None


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def gate_bundle_path(ctx) -> Path:
    """找到当前账号下真正生效的 gate bundle。

    游戏读的是 LocalData 里松散覆盖的那个 bundle（不是 data.unity3d）。定位顺序：
    已知 id（快路）→ 磁盘缓存 → 按文件大小从小到大扫描，第一个命中的就是。
    gate bundle 本身很小（1～2KB），所以扫描通常几百个文件内就停。
    """
    global _GATE_CACHE
    root = ctx.game.root
    key = str(root)

    # 快路：已知 id
    # 判断"是不是当前生效的那份"只有一个可靠依据: 它得覆盖游戏内置表.
    # 游戏换 id 时旧文件可能留在盘上, 光看"里面有 gate"会一直认着过期的那份.
    builtin = {t for t, _ in bundled_gate_records(ctx)}

    def fits(p: Path) -> bool:
        ent = _gate_entries_at(p)
        return ent is not None and {t for t, _ in ent} >= builtin

    for gid in GATE_KNOWN_IDS:
        p = ctx.game.find(gid)
        if p and fits(p):
            _GATE_CACHE = (key, p)
            return p

    if _GATE_CACHE and _GATE_CACHE[0] == key:
        p = Path(_GATE_CACHE[1])
        if p.is_file() and fits(p):
            return p

    cache_file = runtime.data_dir() / "overframe" / "_gate_bundle.json"
    try:
        c = json.loads(cache_file.read_text(encoding="utf-8"))
        if c.get("root") == key and c.get("id"):
            p = ctx.game.find(c["id"])
            if p and fits(p):
                _GATE_CACHE = (key, p)
                return p
    except (OSError, ValueError, KeyError):
        pass

    cands = []
    try:
        for dirpath, _, files in os_walk(root):
            for fn in files:
                p = Path(dirpath) / fn
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                if 0 < sz <= 40000:
                    cands.append((sz, p))
    except OSError:
        pass
    cands.sort(key=lambda x: x[0])
    # 旧版本残留的表条目少、体积也小, 排在前面先被读到. 所以不能"第一个命中就用",
    # 要挑能覆盖当前官方表的那份 (2026-11-05 实测: 1 条的残表排在 69 条的正表前面).
    hits = []
    probe = 0
    for _, p in cands:
        ent = _gate_entries_at(p)
        if ent is None:
            continue
        hits.append((ent, p))
        if {t for t, _ in ent} >= builtin:
            break
        probe += 1
        if probe >= 60:
            # 兜底: 找不到能覆盖的(本机那份本身就是残表), 别为之一口气翻完整个目录
            break
    if not hits:
        raise MDStudioError(tr("over-frame gate bundle not found under %s") % root)
    # 能覆盖官方表的优先, 其次改得新(游戏写过的那份), 再次条目多
    _, p = max(hits, key=lambda h: ({t for t, _ in h[0]} >= builtin,
                                    _mtime(h[1]), len(h[0])))
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"root": key, "id": p.name}),
                              encoding="utf-8")
    except OSError:
        pass
    _GATE_CACHE = (key, p)
    return p


def os_walk(root):
    import os

    return os.walk(root)


def _gate_object(env):
    for o in env.objects:
        if o.type.name != "TextAsset":
            continue
        d = o.read()
        if str(d.m_Name) == GATE_NAME:
            return o
    return None


def _script_bytes(d) -> bytes:
    return (bytes(d.m_Script.encode("utf-8", "surrogateescape"))
            if isinstance(d.m_Script, str) else bytes(d.m_Script))


def _parse_script(raw: bytes) -> list:
    return [struct.unpack("<HH", raw[i:i + REC])
            for i in range(0, len(raw) - len(raw) % REC, REC)]


def gate_records(ctx) -> list:
    """当前账号生效的登记表条目 [(触发卡号, 底图卡号)]。"""
    import UnityPy

    p = gate_bundle_path(ctx)
    env = UnityPy.load(p.read_bytes())
    o = _gate_object(env)
    if o is None:
        return []
    return _parse_script(_script_bytes(o.read()))


def gate_registered(ctx) -> set:
    return {t for t, _ in gate_records(ctx)}


def ours(ctx) -> set:
    """本工具给哪些卡做过超框（journal 里还有记录，撤销后就没有了）。"""
    return {int(r.card) for r in ctx.journal.load()
            if r.card and family_of(r.op) == "overframe"}


def bundled_gate_records(ctx) -> list:
    """游戏内置那份登记表（data.unity3d 里）的条目 [(触发卡号, 底图卡号)]。

    这一份不受我们写入影响：既是判断「官方超框」的依据，也是本机那份松散覆盖文件
    该有的基准。按 data.unity3d 的大小 + 修改时间缓存，游戏更新后自动重建。
    """
    p = ctx.game.dir / "masterduel_Data" / "data.unity3d"
    if not p.is_file():
        return []
    try:
        fp = [p.stat().st_size, int(p.stat().st_mtime)]
    except OSError:
        return []
    cache = runtime.data_dir() / "overframe" / "_official_gate.json"
    if cache.is_file():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("fp") == fp and "recs" in d:
                return [(int(t), int(b)) for t, b in d["recs"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    import UnityPy

    recs = []
    for o in UnityPy.load(p.read_bytes()).objects:
        if o.type.name != "TextAsset":
            continue
        try:
            d = o.read()
        except Exception:
            continue
        if str(d.m_Name) != GATE_NAME:
            continue
        recs = _parse_script(_script_bytes(d))
        break
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(
            {"fp": fp, "recs": [[int(t), int(b)] for t, b in recs]}), encoding="utf-8")
    except OSError:
        pass
    return recs


def bundled_gate_triggers(ctx) -> set:
    return {t for t, _ in bundled_gate_records(ctx)}


def merge_builtin(records, builtin) -> list:
    """把内置表并进本机那份：内置为底，本机独有的追加在后面。

    本机那份是松散覆盖文件，游戏更新改了内置表它不会跟着刷新（实测过 25 条 vs 69 条），
    缺的卡在游戏里就没有超框效果。并表只做加法，不动已有映射。
    """
    out, seen = [], set()
    for t, b in list(builtin) + list(records):
        t = int(t)
        if t in seen:
            continue
        seen.add(t)
        out.append((t, int(b)))
    return out


def official_registered(ctx) -> set:
    """游戏自己给这张卡做的超框：内置那份 + 本地 gate 里除我们加的之外的。"""
    try:
        cur = gate_registered(ctx)
    except MDStudioError:
        cur = set()
    return bundled_gate_triggers(ctx) | (cur - ours(ctx))


def _gate_bytes_with(path: Path, records) -> bytes:
    """把新记录写回该 bundle（只改 m_Script 再 save）。

    同样从字节加载：不占用原文件句柄，后面才能 os.replace 覆盖它。
    """
    import UnityPy

    env = UnityPy.load(path.read_bytes())
    o = _gate_object(env)
    if o is None:
        raise MDStudioError(tr("%s not found in the gate bundle") % GATE_NAME)
    d = o.read()
    payload = b"".join(struct.pack("<HH", int(t), int(b)) for t, b in records)
    d.m_Script = (payload.decode("utf-8", "surrogateescape")
                  if isinstance(d.m_Script, str) else payload)
    d.save()
    return env.file.save(packer="lz4")


def _add_entry(records, card_id, base_id=None):
    cid = int(card_id)
    bid = int(cid if base_id is None else base_id)
    out = [(t, b) for t, b in records if int(t) != cid]
    out.append((cid, bid))
    return out


def _drop_entry(records, card_id):
    cid = int(card_id)
    return [(t, b) for t, b in records if int(t) != cid]


# ------------------------------------------------------------------ 合成
def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """铺满并裁掉多余，保持比例。"""
    r = max(w / img.width, h / img.height)
    img = img.resize((max(1, round(img.width * r)), max(1, round(img.height * r))),
                     Image.LANCZOS)
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _open(path) -> Image.Image:
    p = Path(path)
    if not p.is_file():
        raise MDStudioError(tr("image not found: %s") % path)
    try:
        with Image.open(p) as im:
            return im.convert("RGBA")
    except OSError as e:
        raise MDStudioError(tr("%s: not an image (%s)") % (p.name, e)) from None


def _dim(layer: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return layer
    a = layer.getchannel("A").point(lambda v: round(v * alpha))
    out = layer.copy()
    out.putalpha(a)
    return out


def compose(card_id: int, art=None, subject=None,
            scale: float = 1.0, dx: int = 0, dy: int = 0,
            lore_alpha: float = 1.0, effect: str = EFFECT_PRISMATIC,
            holo_scope=None, out=None, link_markers: int = None) -> Path:
    """合成一张超框卡图，返回写出的 PNG 路径。

    link_markers  连接方向掩码（bit0..7 = LINK_ARROWS 顺序）；None 就自动从游戏
                  CARD_Prop 读这张卡的方向。为 1 的方向画成橙红点亮样式。

    art        卡图背景，Cover 填满插图窗（不给就窗户透明，界面通常会先补一张）
    subject    主体透明 PNG，放大 OVERFLOW_SCALE×scale 后居中压上，跨出框线
    scale      主体额外缩放（1 = 默认冲框比例）
    dx, dy     主体位移（像素）
    lore_alpha 效果框不透明度（1 = 完全盖住主体；<1 = 主体从效果框后面透出来）
    effect     边框效果：prismatic 棱彩 / iridescent 虹彩 / glory 幻彩
               / official 官方 / keep 不改边框
    holo_scope 保留参数，已无实际作用（老调用方还在传）
    """
    nn = frame_of_card(card_id)
    ax, ay, aw, ah = art_box(nn)
    lx, ly, lw, lh = lore_rect(nn)
    effect = normalize_effect(effect)

    cv = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    frame = frame_image(nn, effect)

    # 1) 卡图背景填满插图窗
    if art:
        cv.alpha_composite(_fit_cover(_open(art), aw, ah), (ax, ay))

    # 2) 整框压顶（插图窗透明，画会透出来）
    cv.alpha_composite(frame)

    # 3) 外沿特效：虹彩 / 幻彩 / 官方在最外圈叠素材，盖住游戏原生深色外沿。
    #    放在框之上、主体之下，主体跨出时自然压在特效之上。
    if effect in BORDER_OVERLAY:
        cv.alpha_composite(border_overlay(effect))

    # 4) 连接怪兽的未点亮箭头：铺在主体之下，主体放大后自然把它们盖住。
    #    点亮的那几个要压在主体之上（第 6 步），两层分开合成。预览那边
    #    LinkDimImg 也是放在 SubjectLayer 之下，两边层级必须一致。
    lit = None
    if nn == LINK_FRAME:
        if link_markers is None:
            link_markers = markers_of_card(card_id)
        if link_markers:
            lit, dim = link_arrow_split(nn, effect, link_markers)
            cv.alpha_composite(dim)

    # 5) 主体压上，放大跨出框线；可一直到卡片下沿，效果框会盖住框内那段
    if subject:
        sub = _open(subject)
        cover = max(aw / sub.width, ah / sub.height)
        r = cover * OVERFLOW_SCALE * float(scale or 1.0)
        size = (max(1, round(sub.width * r)), max(1, round(sub.height * r)))
        sub_scaled = sub.resize(size, Image.LANCZOS)
        x = ax + (aw - size[0]) // 2 + int(dx or 0)
        y = ay + (ah - size[1]) // 2 + int(dy or 0)
        layer = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        layer.alpha_composite(sub_scaled, (x, y))
        cv.alpha_composite(layer)

    # 6) 点亮方向的箭头压在主体之上，不会被主体吃掉
    if lit is not None:
        cv.alpha_composite(lit)

    # 7) 效果框单独压到主体之上，透明度可调（1 = 完全遮住主体，边缘不被吃）
    panel = frame.crop((lx, ly, lx + lw, ly + lh))
    cv.alpha_composite(_dim(panel, lore_alpha), (lx, ly))

    dst = Path(out) if out else _default_out(card_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cv.save(dst)
    return dst


def _default_out(card_id: int) -> Path:
    return runtime.data_dir() / "overframe" / f"{int(card_id)}.png"


# ------------------------------------------------------------------ 落盘
def _replace_illust(data: bytes, img: Image.Image) -> bytes:
    """按 704×1024 换掉 bundle 里那张纹理。"""
    from mdstudio.core.bundle import set_image
    import UnityPy

    env = UnityPy.load(data)
    hit = 0
    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        set_image(o.read(), img)
        hit += 1
    if not hit:
        raise MDStudioError(tr("no texture in the artwork bundle"))
    return env.file.save(packer="lz4")


def apply(ctx, card_id: int, image, register_card: bool = True):
    """把合成好的超框图写进游戏，并登记成超框。

    顺序：先写纹理，后改登记表（反过来那一瞬游戏会去读一张还不存在的超框图）。
    登记表读当前生效的 18 条，只加这张卡，写回同一个 gate bundle。
    """
    from mdstudio.ops.tx import Tx

    card_id = int(card_id)
    p = ctx.game.find_logical(illust_logical(card_id))
    if not p:
        raise MDStudioError(tr("card %s has no artwork bundle to write") % card_id)

    img = image.convert("RGBA")
    if img.size != (CARD_W, CARD_H):
        img = img.resize((CARD_W, CARD_H), Image.LANCZOS)

    tx = Tx(ctx, "overframe.set", card_id)
    tx.put_existing(p, _replace_illust(p.read_bytes(), img))
    if register_card:
        gp = gate_bundle_path(ctx)
        recs = _add_entry(gate_records(ctx), card_id)
        tx.put_existing(gp, _gate_bytes_with(gp, recs))
    tx.commit()
    return {"path": str(p), "registered": [card_id] if register_card else []}


def write_bundle(ctx, tx, card_id: int, data: bytes):
    """把一个已经合成好的插图 bundle 原样写进游戏，并登记成超框。

    装整合包用：图是对方烘好的那一张，不需要在本机重新合成。登记表按本机当前
    账号现算，所以包里只带插图本体，不带 gate（每个账号那份的文件名都不一样）。
    """
    card_id = int(card_id)
    tx.put_file(get_crc(illust_logical(card_id)), data)
    gp = gate_bundle_path(ctx)
    recs = _add_entry(gate_records(ctx), card_id)
    tx.put_existing(gp, _gate_bytes_with(gp, recs))


def remove(ctx, card_id: int, drop_register: bool = True):
    """把这张卡的超框图撤掉：纹理与登记表都回到原样。"""
    from mdstudio.ops.tx import undo_ids

    card_id = int(card_id)
    mine = [r for r in ctx.journal.load()
            if r.card == card_id and family_of(r.op) == "overframe"]
    if not mine:
        raise MDStudioError(tr("card %s has no over-frame we made") % card_id)
    undo_ids(ctx, {r.id for r in mine})
    return {"restored": True}


def family_of(op: str) -> str:
    # 整合包装进来的卡图跟自己做的超框是同一件事, 归一族, 撤销/撤回才不会漏掉
    if op == "pack.art":
        return "overframe"
    a, _, _ = op.partition(".")
    return a


def status(ctx, card_id: int) -> dict:
    """这张卡当前的卡图状况，界面上一眼看清"""
    img, meta = read_illust(ctx, card_id)
    if img is None:
        return {"exists": False, "error": meta.get("error")}
    cid = int(card_id)
    try:
        registered = cid in gate_registered(ctx)
    except MDStudioError:
        registered = False
    return {
        "exists": True,
        "width": meta["width"],
        "height": meta["height"],
        "overframe": meta["overframe"],
        "registered": registered,
        "ours": any(r.card == cid and family_of(r.op) == "overframe"
                    for r in ctx.journal.load()),
        "frame": frame_of_card(cid),
        "frame_name": frame_name(frame_of_card(cid)),
    }
