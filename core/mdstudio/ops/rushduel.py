"""Rush Duel 风格卡图 —— 逐卡合成。

跟超框（overframe）是两条线：超框保留 MD 卡框、只让主体跨出框线；这里是把整张卡
换成 Rush Duel 的版式（大插图窗 + 紧凑效果框 + 卡名条 + 等级/ATK/DEF + 属性图标），把卡名、
数值、类型行、效果文字都画进贴图，再写进这张卡的插图槽位并登记超框。

素材来自 RD 目录的 "Rush Duel Card Layout"（Nauder / Floowandereeze & Modding 同人）。
原包的做法是替换 data.unity3d 里的卡面纹理与布局遮罩，是全局改动；这里改成逐卡，只动
一张卡的插图纹理，能走事务与撤销。

素材原始尺寸 813×1185（Rush Duel 卡比例），输出统一缩放到 MD 的 704×1024。
下面所有几何都是 **813 空间**的实测值，用的时候乘 scale。

数据来源：等级 / 属性 / 种族全部从游戏自己的 CARD_Prop 解出来（见 card_info），
不再依赖外部数据库；只有灵摆刻度仍回退到 cards_db.json。
"""

import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from mdstudio import runtime
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr

CARD_W = 704
CARD_H = 1024
RD_W = 813
RD_H = 1185

FRAME_DIR = "tools/rushduel/frame"
ATTR_DIR = "tools/rushduel/attr"   # 从 data.unity3d 的 CardAttributeSet 抠出来的属性字

# ---- 813 空间实测几何（2026-11-02 按 frame 素材 + 卡面截图重新校准）----
# 普通卡插图窗：frame 透明洞 x 42..767 / y 120..847
RD_ART = (42, 120, 726, 728)
# 灵摆卡插图窗只在上半：洞 y 122..570
RD_ART_PEND = (42, 120, 726, 448)
# 卡名条（橙铜色）：实测 y 29..112，文字左对齐、暖白，右侧留出属性图标
RD_NAME = (57, 45, 615, 50)
# 属性图标：卡名条右上端那颗圆标（往左下微调半个半径）
RD_ATTR = (676, 45, 92, 92)
# 等级数字：压在星标正中（星心实测约 (93,791)）
RD_LEVEL_NUM = (70, 752, 66, 80)
# ATK / DEF 数值：各自黑框内、原偏右位置再左移约两个数字宽（黑框 ATK x282..455 / DEF x554..735）
RD_ATK = (282, 792, 126, 46)
RD_DEF = (554, 792, 134, 46)
# 效果框透明度作用的区域（813 空间）：
# 普通卡 = 类型行带 + 效果框整块（frame 实测 y852..1110，x44..764）
RD_PANEL_NORMAL = ((44, 852, 720, 258),)
# 灵摆卡 = 上方灵摆效果带（y580..790）+ 下方怪兽效果框（y855..1100）
RD_PANEL_PEND = ((41, 580, 726, 210), (45, 855, 725, 245))
# 普通卡效果框（类型行 + 效果文本）
RD_LORE = (58, 902, 690, 196)
# 类型行（种族／性质）单独一条带：ATK/DEF 行下方那条略深的框（frame 实测 y850..900，色 #B97046）
RD_TYPE = (58, 852, 700, 46)
# 等级星标 + 攻守梯形条那一条带（压在插图窗下缘的卡框元素）。
# 普通/效果/同调/超量/连接等：插图窗一直延到 y848，主体放大后会盖住星标与攻守灰带，
# 所以要把这条带单独裁出来压在主体之上，和右上属性图标一样永远不被主体覆盖。
# 带内除星标与灰底梯形外都是透明的，叠上去只补这两块图形。
RD_INFO_BAND = (42, 700, 726, 152)
# 灵摆卡：插图窗只在上半，y708..786 是整片米色「灵摆效果」面板（已由可调透明度的
# panel 层管着）。若把上面那整段垫成不透明，会废掉灵摆效果框的透明度滑条。所以灵摆
# 只保护 y786 起的灰底攻守带这一窄条。
RD_INFO_BAND_PEND = (42, 786, 726, 66)
# 灵摆卡：上格灵摆效果（插图下方、数值上方的窄条）、下格类型行 + 怪兽效果（数值下方的大框）
RD_PEND_UPPER = (100, 588, 608, 116)
RD_PEND_LOWER = (54, 850, 700, 236)
# 灵摆刻度数字：直接在蓝/红宝石正上方（宝石实测 BLUE x44..92 y661..698 / RED x716..762 y662..697）
RD_PEND_SCALE_L = (45, 612, 46, 34)
RD_PEND_SCALE_R = (716, 612, 46, 34)
# 主体跨出插图窗的放大系数（与超框页一致的手感）
OVERFLOW_SCALE = 1.38

# MD 的卡框编号 → RD 素材名。
FRAME_BY_PNG = {
    0: "normal", 1: "effect", 2: "ritual", 3: "fusion",
    7: "spell", 8: "trap", 9: "token",
    10: "synchro", 12: "xyz", 18: "link",
    13: "pendn", 14: "pend", 15: "pendx",
    16: "pends", 17: "pendf", 19: "pendr",
}

# 类别 → 类型行中括号里的内容（中文）。卡牌大类由 frame 决定。
CATEGORY_ZH = {
    "normal": "怪兽", "effect": "怪兽/效果", "ritual": "怪兽/仪式", "fusion": "怪兽/融合",
    "synchro": "怪兽/同调", "xyz": "怪兽/超量", "link": "怪兽/连接", "spell": "魔法",
    "trap": "陷阱", "token": "怪兽/衍生物",
    "pendn": "灵摆/怪兽", "pend": "灵摆/怪兽/效果", "pendx": "灵摆/怪兽/超量",
    "pends": "灵摆/怪兽/同调", "pendf": "灵摆/怪兽/融合", "pendr": "灵摆/怪兽/仪式",
}
MONSTER_NAMES = {"normal", "effect", "ritual", "fusion", "synchro", "xyz",
                 "link", "token", "pendn", "pend", "pendx", "pends", "pendf", "pendr"}
PENDULUM_NAMES = {"pendn", "pend", "pendx", "pends", "pendf", "pendr"}

# CARD_Prop 属性码 → 英文 → 中文
ATTR_CODE_EN = {1: "LIGHT", 2: "DARK", 3: "WATER", 4: "FIRE",
                5: "EARTH", 6: "WIND", 7: "DIVINE"}
ATTR_EN_ZH = {"LIGHT": "光", "DARK": "暗", "WATER": "水", "FIRE": "炎",
              "EARTH": "地", "WIND": "风", "DIVINE": "神"}
# CARD_Prop 种族码 → 英文（即 YGO race 枚举），再译中文
RACE_CODE_EN = {
    0: "Normal", 1: "Dragon", 2: "Zombie", 3: "Fiend", 4: "Pyro", 5: "Sea Serpent",
    6: "Rock", 7: "Machine", 8: "Fish", 9: "Dinosaur", 10: "Insect", 11: "Beast",
    12: "Beast-Warrior", 13: "Plant", 14: "Aqua", 15: "Warrior", 16: "Winged Beast",
    17: "Fairy", 18: "Spellcaster", 19: "Thunder", 20: "Reptile", 21: "Psychic",
    22: "Wyrm", 23: "Cyberse", 24: "Divine-Beast", 25: "Illusion",
}
RACE_EN_ZH = {
    "Normal": "通常", "Dragon": "龙", "Zombie": "僵尸", "Fiend": "恶魔", "Pyro": "炎",
    "Sea Serpent": "海龙", "Rock": "岩石", "Machine": "机械", "Fish": "鱼",
    "Dinosaur": "恐龙", "Insect": "昆虫", "Beast": "兽", "Beast-Warrior": "兽战士",
    "Plant": "植物", "Aqua": "水", "Warrior": "战士", "Winged Beast": "鸟兽",
    "Fairy": "天使", "Spellcaster": "魔法师", "Thunder": "雷", "Reptile": "爬行动物",
    "Psychic": "念动力", "Wyrm": "幻龙", "Cyberse": "电子界", "Divine-Beast": "幻神兽",
    "Illusion": "幻想魔",
}
# 属性图标配色（英文属性 → 圆盘色）
ATTR_COLORS = {
    "LIGHT": (240, 226, 120), "DARK": (92, 62, 142), "WATER": (52, 122, 222),
    "FIRE": (222, 72, 52), "EARTH": (150, 112, 62), "WIND": (82, 182, 92),
    "DIVINE": (222, 182, 72),
}
# 属性字素材（英文属性 → attr/ 下的文件名）。魔法/陷阱用 spell/trap。
ATTR_GLYPH = {"LIGHT": "light", "DARK": "dark", "WATER": "water", "FIRE": "fire",
              "EARTH": "earth", "WIND": "wind", "DIVINE": "divine"}
SPELL_COLOR = (46, 150, 96)
TRAP_COLOR = (206, 84, 140)
# 性质（frame 名 → 类型行里的性质词）
SUBTYPE_ZH = {
    "normal": "通常", "effect": "效果", "ritual": "仪式", "fusion": "融合",
    "synchro": "同调", "xyz": "超量", "link": "连接", "token": "衍生物",
    "pendn": "通常", "pend": "效果", "pendx": "超量", "pends": "同调",
    "pendf": "融合", "pendr": "仪式",
}

# 字体：优先系统雅黑，缺失就退到 Arial / PIL 内置
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)
_DIGIT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _load_font(size: int, digits: bool = False):
    for p in (_DIGIT_CANDIDATES if digits else _FONT_CANDIDATES):
        try:
            if Path(p).is_file():
                return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def frame_name(n: int) -> str:
    """按 MD 卡框编号取 RD 素材名。没收录的（异常编号）按效果怪兽处理。"""
    return FRAME_BY_PNG.get(int(n), "effect")


def frame_path(n: int) -> Path:
    p = runtime.bundle_root() / FRAME_DIR / f"{frame_name(n)}.png"
    if not p.is_file():
        raise MDStudioError(tr("rush duel frame not found: %s") % p)
    return p


_frame_cache: dict = {}
_frame_final: dict = {}      # (素材名, 是否灵摆) -> 已缩放、已处理边框效果的卡框
_overlay_final: dict = {}    # 效果名 -> 已缩放到卡面尺寸的特效叠加


def frame_image(n: int) -> Image.Image:
    name = frame_name(n)
    if name not in _frame_cache:
        _frame_cache[name] = Image.open(frame_path(n)).convert("RGBA")
    return _frame_cache[name]


RD_EFFECTS = ("keep", "iridescent", "glory", "official")


def _frame_for(n: int, effect: str = "") -> Image.Image:
    """取缩放好的卡框。RD 素材只有这一套框，效果靠合成时叠素材。"""
    name = frame_name(n)
    key = (name, normalize_effect_name(effect))
    cached = _frame_final.get(key)
    if cached is not None:
        return cached
    img = frame_image(n).resize((CARD_W, CARD_H), Image.LANCZOS)
    if len(_frame_final) > 12:
        _frame_final.clear()
    _frame_final[key] = img
    return img


def normalize_effect_name(effect) -> str:
    """规范效果名；RD 没有棱彩素材，棱彩与认不出的都按不改边框处理。"""
    from mdstudio.ops.overframe import normalize_effect

    e = normalize_effect(effect) if (effect or "").strip() else "keep"
    return e if e in RD_EFFECTS else "keep"


def _overlay_for(effect: str) -> Image.Image | None:
    """边框特效叠加素材，缩放到卡面尺寸后缓存。"""
    from mdstudio.ops.overframe import BORDER_OVERLAY

    ov_path = BORDER_OVERLAY.get(normalize_effect_name(effect))
    if not ov_path:
        return None
    cached = _overlay_final.get(effect)
    if cached is not None:
        return cached
    ov = Image.open(runtime.bundle_root() / ov_path).convert("RGBA")
    ov = ov.resize((CARD_W, CARD_H), Image.LANCZOS)
    _overlay_final[effect] = ov
    return ov


def _scaled() -> tuple:
    return CARD_W / RD_W, CARD_H / RD_H


def _map(box) -> tuple:
    """把 813 空间的矩形换算到 704×1024。"""
    sx, sy = _scaled()
    x, y, w, h = box
    return int(round(x * sx)), int(round(y * sy)), int(round(w * sx)), int(round(h * sy))


def _fit_cover(im: Image.Image, w: int, h: int) -> Image.Image:
    c = max(w / im.width, h / im.height)
    return im.resize((max(1, round(im.width * c)), max(1, round(im.height * c))), Image.LANCZOS)


_band_cache: dict = {}


def _frame_band(category: str):
    """按卡框名裁出「等级星标 + 攻守梯形条」那一条带（已缩放到卡面尺寸）。

    这条带在卡框里压在插图窗下缘，主体放大后会盖住它。把它叠进最上层的信息层，
    就能像右上属性图标那样永远压在主体之上。返回 (band, (x, y))，素材不存在返回 None。
    """
    if not category:
        return None
    if category in _band_cache:
        return _band_cache[category]
    p = runtime.bundle_root() / FRAME_DIR / f"{category}.png"
    out = None
    if p.is_file():
        frame = Image.open(p).convert("RGBA").resize((CARD_W, CARD_H), Image.LANCZOS)
        box = RD_INFO_BAND_PEND if category in PENDULUM_NAMES else RD_INFO_BAND
        x, y, w, h = _map(box)
        out = (frame.crop((x, y, x + w, y + h)), (x, y))
    _band_cache[category] = out
    return out


def _wrap(draw, text: str, font, max_w: float) -> list:
    """按像素宽度折行。中文逐字断，连续的英文/数字整词断，避免把单词劈开。"""
    lines = []
    for para in str(text).replace("\r", "").split("\n"):
        if not para:
            lines.append("")
            continue
        buf = ""
        i = 0
        while i < len(para):
            ch = para[i]
            if ch.isascii() and (ch.isalnum() or ch in "-_'"):
                j = i
                while j < len(para) and para[j].isascii() and (para[j].isalnum() or para[j] in "-_'"):
                    j += 1
                token = para[i:j]
            else:
                token = ch
                j = i + 1
            if draw.textlength(buf + token, font=font) <= max_w or not buf:
                buf += token
            else:
                lines.append(buf)
                buf = token.lstrip() if token.startswith(" ") else token
            i = j
        lines.append(buf)
    return lines


def _draw_text_block(cv: Image.Image, box, text: str, max_size: int, min_size: int,
                     fill=(24, 18, 12), line_gap: float = 1.20) -> None:
    """往 box 里塞一段自动折行、自动缩号的文字。放不下就把字号往下压，压到底截断。"""
    x, y, w, h = box
    if not text or w <= 4 or h <= 4:
        return
    draw = ImageDraw.Draw(cv)
    size = max_size
    lines = []
    while size >= min_size:
        font = _load_font(size)
        lines = _wrap(draw, text, font, w)
        lh = int(size * line_gap)
        if len(lines) * lh <= h:
            break
        size -= 1
    font = _load_font(max(size, min_size))
    lh = int(max(size, min_size) * line_gap)
    max_lines = max(1, h // lh)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > w:
            last = last[:-1]
        lines[-1] = last + "…"
    cy = y
    for ln in lines:
        draw.text((x, cy), ln, font=font, fill=fill)
        cy += lh


def _draw_left(cv: Image.Image, box, text: str, size: int, fill) -> None:
    """左对齐 + 自适应缩号，文字靠 box 左边缘起排。"""
    x, y, w, h = box
    if not text:
        return
    draw = ImageDraw.Draw(cv)
    font = _load_font(size)
    while size > 10:
        bb = draw.textbbox((0, 0), text, font=font)
        if bb[2] - bb[0] <= w:
            break
        size -= 1
        font = _load_font(size)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((x - bb[0], y + (h - th) // 2 - bb[1]), text, font=font, fill=fill)


def _draw_right(cv: Image.Image, box, text: str, size: int, fill) -> None:
    """数值右对齐贴边，像卡面数字那样靠右。"""
    x, y, w, h = box
    if not text:
        return
    draw = ImageDraw.Draw(cv)
    font = _load_font(size, digits=True)
    while size > 10:
        bb = draw.textbbox((0, 0), text, font=font)
        if bb[2] - bb[0] <= w:
            break
        size -= 1
        font = _load_font(size, digits=True)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((x + w - tw - bb[0], y + (h - th) // 2 - bb[1]), text, font=font, fill=fill)


def _draw_centered(cv: Image.Image, box, text: str, size: int, fill) -> None:
    x, y, w, h = box
    if not text:
        return
    draw = ImageDraw.Draw(cv)
    font = _load_font(size)
    while size > 10:
        bb = draw.textbbox((0, 0), text, font=font)
        if bb[2] - bb[0] <= w:
            break
        size -= 1
        font = _load_font(size)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((x + (w - tw) // 2 - bb[0], y + (h - th) // 2 - bb[1]), text, font=font, fill=fill)


_attr_cache: dict = {}


def _glyph_image(key: str):
    if key not in _attr_cache:
        p = runtime.bundle_root() / ATTR_DIR / f"{key}.png"
        _attr_cache[key] = Image.open(p).convert("RGBA") if p.is_file() else None
    return _attr_cache[key]


def _draw_attribute_emblem(cv: Image.Image, box, attribute_en: str, category: str = "") -> None:
    """右上角属性图标：彩色圆盘 + 从 data.unity3d 抠出来的属性字。

    怪兽用 光/暗/水/炎/地/风/神，魔法用 魔（绿）、陷阱用 罠（粉）。
    """
    if category == "spell":
        key, color = "spell", SPELL_COLOR
    elif category == "trap":
        key, color = "trap", TRAP_COLOR
    else:
        key, color = ATTR_GLYPH.get(attribute_en), ATTR_COLORS.get(attribute_en)
    if not key or not color:
        return
    glyph = _glyph_image(key)
    if glyph is None:
        return
    ex, ey, ew, eh = box
    cx, cy = ex + ew // 2, ey + eh // 2
    r = min(ew, eh) // 2 - 2
    draw = ImageDraw.Draw(cv)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color + (255,))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(240, 240, 240), width=2)
    inner = max(8, int(r * 1.55))
    gw, gh = glyph.size
    c = min(inner / gw, inner / gh)
    gw2, gh2 = max(1, int(gw * c)), max(1, int(gh * c))
    g = glyph.resize((gw2, gh2), Image.LANCZOS)
    # 浅底（光/神）用深色字，其余白字
    if attribute_en in ("LIGHT", "DIVINE") and category not in ("spell", "trap"):
        dark = Image.new("RGBA", g.size, (45, 30, 12, 0))
        dark.putalpha(g.getchannel("A"))
        g = dark
    cv.alpha_composite(g, (cx - gw2 // 2, cy - gh2 // 2))


def _draw_level_number(cv: Image.Image, box, level) -> None:
    """等级数字压在星标图标上：白字 + 深色描边，保留原星标。"""
    if level is None:
        return
    x, y, w, h = box
    text = str(level)
    draw = ImageDraw.Draw(cv)
    size = 48
    font = _load_font(size)
    while size > 12:
        bb = draw.textbbox((0, 0), text, font=font)
        if bb[2] - bb[0] <= w and bb[3] - bb[1] <= h:
            break
        size -= 1
        font = _load_font(size)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((x + (w - tw) // 2 - bb[0], y + (h - th) // 2 - bb[1]), text,
              font=font, fill=(255, 255, 255), stroke_width=3, stroke_fill=(40, 20, 10))


def _draw_scale(cv: Image.Image, box, value) -> None:
    if not value:
        return
    _draw_centered(cv, box, str(value), 26, (20, 20, 20))


_info_cache: dict = {}
_desc_table = None
_cards_db: dict = None
_prop_blob_cache: dict = {}


def _load_desc_table(game) -> list:
    """CARD_Desc 解出来是一串 \\0 分隔的文本，条数与卡名表一一对应（14254 条）。

    解密一次要 1.7 秒左右，所以整个进程只做一次。
    """
    global _desc_table
    if _desc_table is not None:
        return _desc_table
    from mdstudio.core import cards as C

    folder, lang = _card_data_dir()
    if not (folder and lang):
        _desc_table = []
        return _desc_table
    indx_enc = C._textasset(game.find_logical(
        f"Card/Data/{folder}/{lang}/CARD_Indx").read_bytes())
    key = C.find_key(indx_enc)
    raw = C.text_decrypt(C._textasset(game.find_logical(
        f"Card/Data/{folder}/{lang}/CARD_Desc").read_bytes()), key)
    _desc_table = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
    return _desc_table


def _card_data_dir():
    from mdstudio.ops.overframe import _card_data_dir as f

    return f()


def _load_cards_db() -> dict:
    global _cards_db
    if _cards_db is not None:
        return _cards_db
    p = Path(__file__).parent.parent.parent / "tools" / "rushduel" / "cards_db.json"
    try:
        _cards_db = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _cards_db = {}
    return _cards_db


def _card_prop(ctx, card_id: int):
    """解 CARD_Prop，返回这张卡的原始字段。

    每条记录 8 字节：id(2) + tb(1) + b3(1) + packed(4)。实测：
      atk  = (packed & 0x1FF) * 10
      def  = ((packed >> 9) & 0x1FF) * 10
      link = tb&0x20 且 tb&0x0F in (0x0A,0x0B)
      level= (b3 >> 2) & 0xF
      attr = ((P >> 6) & 0xF)        P = 整条 48 位
      race = ((P >> 37) & 0x1F)
    魔法/陷阱没有攻防，也没有属性（attr 码落在 0/8/9），种族码 0 表示其副类（通常/永续…）。
    """
    from mdstudio.core import cards as C

    folder, lang = _card_data_dir()
    if not (folder and lang):
        return None
    key = (folder, lang)
    dec = _prop_blob_cache.get(key)
    if dec is None:
        enc = C._textasset(ctx.game.find_logical(
            f"Card/Data/{folder}/{lang}/CARD_Prop").read_bytes())
        dec = C.text_decrypt(enc, C.find_key(enc))
        _prop_blob_cache[key] = dec
    cid = int(card_id)
    for i in range(8, len(dec) - 7, 8):
        if (dec[i] | (dec[i + 1] << 8)) != cid:
            continue
        tb = dec[i + 2]
        b3 = dec[i + 3]
        p0, p1, p2, p3 = dec[i + 4], dec[i + 5], dec[i + 6], dec[i + 7]
        packed = p0 | (p1 << 8) | (p2 << 16) | (p3 << 24)
        P = tb | (b3 << 8) | (p0 << 16) | (p1 << 24) | (p2 << 32) | (p3 << 40)
        return {
            "tb": tb, "b3": b3, "packed": packed, "P": P,
            "atk": (packed & 0x1FF) * 10,
            "def": ((packed >> 9) & 0x1FF) * 10,
            "atk_raw": packed & 0x1FF,
            "def_raw": (packed >> 9) & 0x1FF,
            "is_link": bool(tb & 0x20) and (tb & 0x0F) in (0x0A, 0x0B),
            "level": (b3 >> 2) & 0xF,
            "attr_code": (P >> 6) & 0xF,
            "race_code": (P >> 37) & 0x1F,
            "pend_scale": (P >> 43) & 0xF,
        }
    return None


def card_info(ctx, card_id: int) -> dict:
    """这张卡画到 RD 卡面上需要的东西。

    卡名 / 描述来自游戏数据；等级 / 属性 / 种族从 CARD_Prop 实时解（覆盖 100%，
    含 MD 独占卡）。CARD_Prop 解不到时回退 cards_db.json。
    """
    cid = int(card_id)
    if cid in _info_cache:
        return _info_cache[cid]
    from mdstudio.core import cards as C
    from mdstudio.ops.overframe import frame_of_card

    db = C.CardDB(ctx.game).load()
    name = db.name(cid)
    desc_table = _load_desc_table(ctx.game)
    order = list(db.names.keys())
    desc = ""
    try:
        i = order.index(cid)
        if i < len(desc_table):
            desc = desc_table[i]
    except ValueError:
        pass

    atk = dfn = None
    is_link = False
    level = None
    link_rating = 0
    attr_en = ""
    race_en = ""
    scale = 0
    folder, lang = _card_data_dir()
    dec = _card_prop(ctx, cid) if (folder and lang) else None
    if dec:
        # 魔法／陷阱的攻防域是别的位域，不能当数值用
        is_spell_trap = (dec["tb"] & 0x0F) in (0x0D, 0x4E) and (dec["b3"] == 0x02)
        is_link = dec["is_link"]
        level = dec["level"]
        link_rating = dec["level"] if is_link else 0
        attr_en = ATTR_CODE_EN.get(dec["attr_code"], "")
        race_en = RACE_CODE_EN.get(dec["race_code"], "")
        if not is_spell_trap:
            ar = dec["atk_raw"]
            dr = dec["def_raw"]
            # 0x1FF 是「？」的哨兵（511*10 会显示成 5110，所以单独判）
            atk = "?" if ar == 0x1FF else ar * 10
            if is_link:
                # 连接卡没有守备力，显示 "Link - N"（N 存于 level 位）
                dfn = f"Link - {link_rating}"
            else:
                dfn = "?" if dr == 0x1FF else dr * 10

    category = frame_name(frame_of_card(cid))
    is_monster = category in MONSTER_NAMES
    is_pend = category in PENDULUM_NAMES
    # 非怪兽没有属性 / 种族，清掉，免得类型行乱带
    if not is_monster:
        attr_en = ""
        race_en = ""
    # 连接卡不画等级星（level 位给的是连接值，已用于 Link-N）
    if is_link:
        level = None
    # 灵摆刻度只在灵摆卡上取（非灵摆卡那几个位是别的值）
    if is_pend and dec and dec.get("pend_scale"):
        scale = dec["pend_scale"]

    # 回退：CARD_Prop 解不到的字段，用 cards_db 补（含灵摆刻度）
    if level is None or not attr_en or not race_en or (is_pend and not scale):
        cd = _load_cards_db().get(str(cid))
        if cd:
            if level is None and not is_link:
                level = cd[3] or None
            if not attr_en:
                attr_en = cd[2]
            if not race_en:
                race_en = cd[1]
            if is_pend and not scale:
                scale = cd[5] or 0

    info = {"name": name, "desc": desc, "atk": atk, "def": dfn, "link": is_link,
            "level": level, "attribute_en": attr_en, "race_en": race_en,
            "scale": scale, "category": category, "is_pendulum": category in PENDULUM_NAMES,
            "is_monster": is_monster}
    _info_cache[cid] = info
    return info


def _type_line(category: str, race_en: str, attribute_en: str) -> str:
    """类型行，照参考卡写成【种族族／性质】。魔法/陷阱写【魔法卡】/【陷阱卡】。"""
    if category == "spell":
        return "【魔法卡】"
    if category == "trap":
        return "【陷阱卡】"
    sub = SUBTYPE_ZH.get(category, "效果")
    race = (RACE_EN_ZH.get(race_en, race_en) + "族") if race_en else ""
    if category in PENDULUM_NAMES:
        body = f"{race}／灵摆／{sub}" if race else f"灵摆／{sub}"
    else:
        body = f"{race}／{sub}" if race else sub
    return f"【{body}】"


def _split_pendulum(desc: str):
    """灵摆卡描述：返回 (灵摆效果文本, 怪兽效果文本)。

    约定：【灵摆效果】之前是怪兽效果、之后是灵摆效果；怪兽部分若带【怪兽效果】
    标记则去掉。没有标记就把整段当怪兽效果。
    """
    marker = "【灵摆效果】"
    if marker in desc:
        idx = desc.index(marker)
        monster = desc[:idx]
        pend = desc[idx + len(marker):]
        mg = "【怪兽效果】"
        if mg in monster:
            monster = monster[monster.index(mg) + len(mg):]
        return pend.strip(), monster.strip()
    return "", desc.strip()


def compose(card_id: int, art, name: str = "", atk=None, dfn=None, level=None,
            desc: str = "", category: str = "", attribute_en: str = "", race_en: str = "",
            pendulum_scale: int = 0, subject=None, scale: float = 1.0, dx: int = 0,
            dy: int = 0, effect: str = "", frame_alpha: float = 1.0, out=None) -> Path:
    """合成一张 Rush Duel 风格的卡图（704×1024），返回写出的 PNG 路径。

    art         卡图背景，Cover 铺满插图窗（不给就窗户保持黑底）
    name        卡名（左对齐、暖白）
    atk/dfn     攻击力/守备力（int 或字符串）；None 就不画（魔法／陷阱卡）
    level       等级（int）；None 不画
    desc        效果文本（自动折行、自动缩号）
    category    frame 素材名，决定普通/灵摆版式与类型行
    attribute_en/race_en 用来拼类型行、画属性图标
    pendulum_scale 灵摆刻度（仅灵摆卡用）
    subject     主体透明 PNG，放大 OVERFLOW_SCALE×scale 后居中压上，可跨出框线
    scale/dx/dy 主体额外缩放与位移
    effect      边框特效（棱彩/虹彩/荣耀/官方/原生），复用超框的叠加素材
    frame_alpha 效果框不透明度（1=不透明；调低时压在下面的主体从框里透出来）
    """
    base, info, sub, sub_xy, panel, _ = _build_layers(
        card_id, art, name, atk, dfn, level, desc, category, attribute_en, race_en,
        pendulum_scale, subject, scale, effect, frame_alpha)
    if sub is not None:
        lay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        lay.alpha_composite(sub, (sub_xy[0] + int(dx or 0), sub_xy[1] + int(dy or 0)))
        base.alpha_composite(lay)
    try:
        fa = max(0.0, min(1.0, float(frame_alpha)))
    except (TypeError, ValueError):
        fa = 1.0
    if panel is not None:
        if fa < 1.0:
            panel = panel.copy()
            panel.putalpha(panel.getchannel("A").point(lambda v: int(v * fa)))
        base.alpha_composite(panel)
    base.alpha_composite(info)

    dst = Path(out) if out else (runtime.data_dir() / "rushduel" / f"{int(card_id)}.png")
    dst.parent.mkdir(parents=True, exist_ok=True)
    base.save(dst)
    return dst


def _build_layers(card_id, art, name, atk, dfn, level, desc, category, attribute_en,
                  race_en, pendulum_scale, subject, scale, effect, frame_alpha,
                  stats=None, preview=False):
    """把整卡拆成层：base（卡图+卡框，效果框那块挖掉）、panel（效果框那一块）、
    info（信息层）、sub（主体）。

    层序：base → sub → panel → info。效果框压在主体之上，所以调低它的不透明度时，
    和它重叠的主体就从下面透出来（跟超框一个意思），而不是透出黑底发灰。
    stats 可选，回填 info_hit / sub_hit，让预览知道哪层没变、不用重存。
    """
    from mdstudio.ops.overframe import frame_of_card

    if not category:
        category = frame_name(frame_of_card(int(card_id)))
    is_pend = category in PENDULUM_NAMES
    base = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 255))
    ax, ay, aw, ah = _map(RD_ART_PEND if is_pend else RD_ART)
    if art:
        im = Image.open(art).convert("RGB") if not isinstance(art, Image.Image) else art.convert("RGB")
        fitted = _fit_cover(im, aw, ah)
        base.paste(fitted, (ax + (aw - fitted.width) // 2, ay + (ah - fitted.height) // 2))

    nn = frame_of_card(int(card_id))
    frame_rest, panel = _split_panel(_frame_for(nn, effect), is_pend,
                                     (frame_name(nn), normalize_effect_name(effect)))
    base.alpha_composite(frame_rest)

    ov = _overlay_for(effect)
    if ov is not None:
        base.alpha_composite(ov)

    sub = None
    sub_xy = (0, 0)
    sub_size = (0, 0)
    if subject:
        s = Image.open(subject).convert("RGBA") if not isinstance(subject, Image.Image) \
            else subject.convert("RGBA")
        cover = max(aw / s.width, ah / s.height)
        r = cover * OVERFLOW_SCALE * float(scale or 1.0)
        # 逻辑尺寸随倍率线性放大；预览只把位图分辨率压到上限，前端再用逻辑尺寸拉伸显示，
        # 这样倍率不会像以前那样被 1400px 卡住不再变大
        logical = (max(1, round(s.width * r)), max(1, round(s.height * r)))
        sub_size = logical
        ck = (str(subject) if not isinstance(subject, Image.Image) else id(subject),
              logical, preview)
        cached_sub = _sub_layer_cache.get(ck)
        if cached_sub is not None:
            sub = cached_sub
            if stats is not None:
                stats["sub_hit"] = True
        else:
            if preview and max(logical) > PREVIEW_SUB_MAX:
                k = PREVIEW_SUB_MAX / max(logical)
                bmp = (max(1, round(logical[0] * k)), max(1, round(logical[1] * k)))
            else:
                bmp = logical
            sub = s.resize(bmp, Image.LANCZOS)
            if len(_sub_layer_cache) > 3:
                _sub_layer_cache.clear()
            _sub_layer_cache[ck] = sub
        sub_xy = (ax + (aw - logical[0]) // 2, ay + (ah - logical[1]) // 2)

    info = _info_layer(name, atk, dfn, level, desc, category, attribute_en, race_en,
                       pendulum_scale, stats)
    return base, info, sub, sub_xy, panel, sub_size


_split_cache: dict = {}


def _split_panel(frame: Image.Image, is_pend: bool, key):
    """把卡框切成「效果框那一块」和「其余部分」，两块拼回去和原图一致。

    效果框单独成层，前端才能只对它调不透明度（主体压在它下面）。
    """
    cached = _split_cache.get(key)
    if cached is not None:
        return cached
    mask = Image.new("L", (CARD_W, CARD_H), 0)
    md = ImageDraw.Draw(mask)
    for box in (RD_PANEL_PEND if is_pend else RD_PANEL_NORMAL):
        bx, by, bw, bh = _map(box)
        md.rectangle([bx, by, bx + bw, by + bh], fill=255)
    alpha = frame.getchannel("A")
    panel = frame.copy()
    panel.putalpha(ImageChops.multiply(alpha, mask))
    rest = frame.copy()
    rest.putalpha(ImageChops.multiply(alpha, ImageChops.invert(mask)))
    if len(_split_cache) > 8:
        _split_cache.clear()
    _split_cache[key] = (rest, panel)
    return rest, panel


_info_layer_cache: dict = {}
_sub_layer_cache: dict = {}


def _info_layer(name, atk, dfn, level, desc, category, attribute_en, race_en,
                pendulum_scale, stats=None) -> Image.Image:
    """信息层（卡名/属性/等级/攻防/类型行/效果文字）。参数没变就直接复用上次的图。

    切边框效果、拖透明度滑条时信息层不用重画，省掉文字排版与一次 PNG 编码。
    """
    key = (name, atk, dfn, level, desc, category, attribute_en, race_en, pendulum_scale)
    cached = _info_layer_cache.get(key)
    if cached is not None:
        if stats is not None:
            stats["info_hit"] = True
        return cached

    info = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    # 先把「等级星标 + 攻守梯形条」这条卡框带垫在信息层最底部。信息层整体压在主体
    # 之上，所以这条带（以及随后画的等级数字、攻防数值）都不会被放大的主体盖住。
    bandinfo = _frame_band(category)
    if bandinfo is not None:
        band, (bx, by) = bandinfo
        info.alpha_composite(band, (bx, by))
    if name:
        _draw_left(info, _map(RD_NAME), name, 40, (250, 245, 238))
    _draw_attribute_emblem(info, _map(RD_ATTR), attribute_en, category)
    if category in MONSTER_NAMES:
        _draw_level_number(info, _map(RD_LEVEL_NUM), level)
    if atk is not None:
        _draw_right(info, _map(RD_ATK), str(atk), 34, (255, 255, 255))
    if dfn is not None:
        _draw_right(info, _map(RD_DEF), str(dfn), 34, (255, 255, 255))
    tl = _type_line(category, race_en, attribute_en)
    if category in PENDULUM_NAMES:
        pend_effect, monster_effect = _split_pendulum(desc)
        upper = (pend_effect if pend_effect else desc.strip())
        lower = (monster_effect if monster_effect else desc.strip())
        if upper:
            _draw_text_block(info, _map(RD_PEND_UPPER), upper, 17, 11)
        lx, ly, lw, lh = _map(RD_PEND_LOWER)
        # 类型行与普通卡同字号（26），不再用效果正文的 17 号
        _draw_left(info, (lx, ly, lw, 44), tl, 26, (18, 18, 18))
        # 怪兽效果文本再往下约一个文字高，和类型行拉开一点
        if lower:
            _draw_text_block(info, (lx, ly + 46, lw, max(10, lh - 46)), lower, 17, 11)
        if pendulum_scale:
            _draw_scale(info, _map(RD_PEND_SCALE_L), pendulum_scale)
            _draw_scale(info, _map(RD_PEND_SCALE_R), pendulum_scale)
    else:
        _draw_left(info, _map(RD_TYPE), tl, 26, (18, 18, 18))
        if desc.strip():
            _draw_text_block(info, _map(RD_LORE), desc.strip(), 19, 11)
    if len(_info_layer_cache) > 6:
        _info_layer_cache.clear()
    _info_layer_cache[key] = info
    return info


PREVIEW_SUB_MAX = 1400   # 预览主体位图最长边上限，太大时前端拖动会卡（逻辑尺寸不受限）


def layers(card_id: int, art, name: str = "", atk=None, dfn=None, level=None,
           desc: str = "", category: str = "", attribute_en: str = "", race_en: str = "",
           pendulum_scale: int = 0, subject=None, scale: float = 1.0, effect: str = "",
           frame_alpha: float = 1.0, out_dir=None) -> dict:
    """预览用分层：底图 / 主体 / 效果框 / 信息层 分别出 PNG。

    层序 base → subject → panel → info。主体坐标以 dx=dy=0 为基准，前端拖动时本地
    平移即可；效果框的不透明度由前端直接设在 panel 图层上，拖滑条不用再找引擎。
    最终写入仍走 compose（把各层合成成一张）。
    """
    stats: dict = {}
    base, info, sub, sub_xy, panel, sub_size = _build_layers(
        card_id, art, name, atk, dfn, level, desc, category, attribute_en, race_en,
        pendulum_scale, subject, scale, effect, frame_alpha, stats, preview=True)
    d = Path(out_dir) if out_dir else (runtime.data_dir() / "rushduel" / "layers" / str(int(card_id)))
    d.mkdir(parents=True, exist_ok=True)
    bp = d / "base.png"
    ip = d / "info.png"
    pp = d / "panel.png"
    base.save(bp, compress_level=1)
    # 信息层参数没变、文件还在就直接复用（切边框时省掉一次编码）
    if not (stats.get("info_hit") and ip.is_file()):
        info.save(ip, compress_level=1)
    sp = ""
    if sub is not None:
        sp = str(d / "sub.png")
        if not (stats.get("sub_hit") and Path(sp).is_file()):
            sub.save(sp, compress_level=1)
    if panel is not None:
        panel.save(pp, compress_level=1)
    return {"base": str(bp), "info": str(ip), "subject": sp,
            "panel": str(pp) if panel is not None else "",
            "panel_stamp": str(pp.stat().st_mtime_ns) if panel is not None else "",
            "sub_x": int(sub_xy[0]), "sub_y": int(sub_xy[1]),
            "sub_w": int(sub_size[0]) if sub is not None else 0,
            "sub_h": int(sub_size[1]) if sub is not None else 0,
            "base_stamp": str(bp.stat().st_mtime_ns),
            "info_stamp": str(ip.stat().st_mtime_ns),
            "sub_stamp": str(Path(sp).stat().st_mtime_ns) if sp else "",
            "card_w": CARD_W, "card_h": CARD_H}


# ------------------------------------------------------------------ 写入 / 移除
def apply(ctx, card_id: int, image, register_card: bool = True) -> dict:
    """把合成好的 RD 卡图写进游戏并登记超框。

    直接复用超框的写入与登记（同一个插图槽位、同一张 gate 表）：RD 风格卡在
    游戏里就是一张「整卡自绘」的超框图，原生名字/攻防/效果文字都会被抑制，
    位置完全由贴图决定。撤销历史也归到 overframe 家族，History 页通用。
    """
    from mdstudio.ops import overframe as OF

    return OF.apply(ctx, card_id, image, register_card=register_card)


def remove(ctx, card_id: int) -> dict:
    """撤掉这张卡的 RD 卡图（等价于撤掉它的超框）。"""
    from mdstudio.ops import overframe as OF

    return OF.remove(ctx, card_id)


def status(ctx, card_id: int) -> dict:
    """这张卡的 RD 状况：预览在不在、有没有登记、登记是不是我们写的。"""
    from mdstudio.ops import overframe as OF

    return OF.status(ctx, card_id)


# ------------------------------------------------------------------ 编辑参数存档
# 跟超框的 edits.json 同一套思路：写入成功后把素材与参数记下来，
# 下次打开这张卡自动还原，方便在原有基础上继续调。

def _edits_file() -> Path:
    return runtime.data_dir() / "rushduel" / "edits.json"


def _edits_dir() -> Path:
    d = runtime.data_dir() / "rushduel" / "edits"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_edits() -> dict:
    p = _edits_file()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _copy_in(src, card_id: int, suffix: str) -> str:
    """把素材复制进 edits 目录，避免依赖外部原文件。"""
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


def load_edit(card_id: int) -> dict:
    return _load_edits().get(str(int(card_id)), {})


def save_edit(ctx, card_id: int, art, art_name, subject, subject_name,
              scale, dx, dy, effect: str = "", frame_alpha: float = 1.0) -> None:
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
    entry["effect"] = normalize_effect_safe(effect)
    try:
        entry["frame_alpha"] = round(float(frame_alpha), 2)
    except (TypeError, ValueError):
        entry["frame_alpha"] = 1.0
    data[str(int(card_id))] = entry
    try:
        f = _edits_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def normalize_effect_safe(effect):
    try:
        from mdstudio.ops.overframe import normalize_effect
        return normalize_effect(effect)
    except Exception:
        return effect or ""


def clear_edit(ctx, card_id: int) -> None:
    data = _load_edits()
    if str(int(card_id)) in data:
        data.pop(str(int(card_id)), None)
        try:
            _edits_file().write_text(json.dumps(data, ensure_ascii=False),
                                     encoding="utf-8")
        except OSError:
            pass
