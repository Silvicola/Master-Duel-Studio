"""Rush Duel 全局外观 —— 把游戏自己的卡框纹理与布局遮罩换掉。

跟超框（overframe）是互斥的两种做法：
  超框   逐卡合成一张 MD 版式的图，写进那张卡的插图槽位，登记 of_card_asset
  RD 主题 换掉 data.unity3d 里的卡框纹理与布局遮罩，所有卡一起变 RD 版式，
         卡名／攻防／效果文字仍由游戏自己按新布局绘制

所以这里的替换是**全局**的，写在共享的 masterduel_Data/data.unity3d，不按账号分。
因为要动这个 128MB 的大文件，写入前会把整份文件备份一份；还原就是把备份写回去。

素材（随包在 tools/rushduel/）：
  frame/  16 张 RD 卡框，813×1185，写进去前缩放到卡面原本的尺寸
  mask/   8 张 RD 布局遮罩，尺寸与游戏里的一致，原样写入

纹理靠**名字**匹配（游戏里就叫 card_frame00 / CardMask_N 这些），不需要 path_id 表。
"""

import json
import shutil
from pathlib import Path

from PIL import Image

from mdstudio import runtime
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr

FRAME_DIR = "tools/rushduel/frame"
MASK_DIR = "tools/rushduel/mask"

DATA_UNITY3D = "masterduel_Data/data.unity3d"
# 备份与标记都放在工具自己的数据目录里，跟游戏的 LocalData 分开放
BACKUP_NAME = "data.unity3d.rd.bak"
MARK_NAME = "rushduel.json"

# MD 卡框编号 → RD 素材名。编号含义见 overframe.CODE_TO_PNG 的注释。
FRAME_MAP = {
    "card_frame00": "normal",    # 通常
    "card_frame01": "effect",    # 效果
    "card_frame02": "ritual",    # 仪式
    "card_frame03": "fusion",    # 融合
    "card_frame07": "spell",     # 魔法
    "card_frame08": "trap",      # 陷阱
    "card_frame09": "token",     # 衍生物
    "card_frame10": "synchro",   # 同调
    "card_frame12": "xyz",       # 超量
    "card_frame13": "pendn",     # 灵摆通常
    "card_frame14": "pend",      # 灵摆效果
    "card_frame15": "pendx",     # 灵摆超量
    "card_frame16": "pends",     # 灵摆同调
    "card_frame17": "pendf",     # 灵摆融合
    "card_frame18": "link",      # 连接
    "card_frame19": "pendr",     # 灵摆仪式
}

# 游戏纹理名 → RD 遮罩素材。原包只给了这 8 张；游戏里的 Manga 那两套
# （CardMask_N_Manga / CardNormal_RNA_Manga）原作者没处理，这里也不动。
MASK_MAP = {
    "CardMask_L": "CardMask_L.png",
    "CardMask_N": "CardMask_N.png",
    "CardMask_P": "CardMask_P.png",
    "CardNormal_RNA": "CardNormal_RNA.png",
    "CardNormal_Link": "CardNormal_Link.png",
    "CardNormal_Pen": "CardNormal_Pen.png",
    "CardNormal_RNA_Sp": "CardNormal_RNA_Sp.png",
    "GUI_T_Icon1_Mask_Level": "GUI_T_Icon1_Mask_Level.png",
}


def backup_dir() -> Path:
    d = runtime.data_dir() / "appearance"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unity_path(game) -> Path:
    # game.root 是 LocalData/<账号>/0000，data.unity3d 在游戏安装目录下，
    # 且不分账号（全游戏共用这一份），所以用 game.dir。
    p = Path(game.dir) / DATA_UNITY3D
    if not p.is_file():
        raise MDStudioError(tr("data.unity3d not found: %s") % p)
    return p


def _mark() -> dict:
    p = backup_dir() / MARK_NAME
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def status(game) -> dict:
    """当前是否已应用 RD 主题。看备份文件与标记是否都在。"""
    m = _mark()
    bak = backup_dir() / BACKUP_NAME
    applied = bool(m.get("applied")) and bak.is_file()
    return {
        "applied": applied,
        "backup": str(bak) if bak.is_file() else "",
        "backup_mb": round(bak.stat().st_size / 1048576, 1) if bak.is_file() else 0,
        "applied_at": m.get("applied_at", ""),
        "frames": len(FRAME_MAP),
        "masks": len(MASK_MAP),
    }


def plan(game) -> dict:
    """扫一遍 data.unity3d，报出将要替换哪些纹理、各自多大。只读，不改文件。"""
    import UnityPy

    env = UnityPy.load(str(_unity_path(game)))
    hits = {"frames": [], "masks": []}
    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        try:
            d = o.read()
        except Exception:
            continue
        nm = d.m_Name or ""
        if nm in FRAME_MAP:
            hits["frames"].append({"name": nm, "w": d.m_Width, "h": d.m_Height})
        elif nm in MASK_MAP:
            hits["masks"].append({"name": nm, "w": d.m_Width, "h": d.m_Height})
    return hits


def preview(out=None, art=None) -> Path:
    """出一张示意卡：RD 卡框 + 卡图。

    这里不画文字。文字位置由游戏的布局遮罩决定，我们复刻不准，画上去会误导。
    """
    base = runtime.bundle_root()
    frame = Image.open(base / FRAME_DIR / "effect.png").convert("RGBA")
    frame = frame.resize((704, 1024), Image.LANCZOS)

    cv = Image.new("RGBA", (704, 1024), (0, 0, 0, 255))
    ax, ay, aw, ah = 36, 103, 628, 577
    if art and Path(art).is_file():
        im = Image.open(art).convert("RGB")
        c = max(aw / im.width, ah / im.height)
        im = im.resize((max(1, round(im.width * c)), max(1, round(im.height * c))), Image.LANCZOS)
        cv.paste(im, (ax + (aw - im.width) // 2, ay + (ah - im.height) // 2))
    else:
        # 没给卡图时铺个浅色占位，别让插图窗整块黑着，看不出这是窗
        from PIL import ImageDraw

        cv.paste(Image.new("RGB", (aw, ah), (232, 233, 236)), (ax, ay))
        d = ImageDraw.Draw(cv)
        d.text((ax + aw / 2 - 32, ay + ah / 2 - 8), tr("artwork"), fill=(120, 124, 132))
    cv.alpha_composite(frame)

    dst = Path(out) if out else (backup_dir() / "preview.png")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cv.save(dst)
    return dst


def _same_image(a: Image.Image, b: Image.Image) -> bool:
    if a.size != b.size:
        return False
    return a.convert("RGBA").tobytes() == b.convert("RGBA").tobytes()


def apply_theme(ctx) -> dict:
    """备份 data.unity3d，然后把卡框与遮罩写进去。

    卡框素材是 813×1185，这里缩放到目标纹理原本的尺寸（704×1024 或 480×700），
    不动 m_Width/m_Height。理由是布局遮罩按 UV 比例映射，改尺寸没有必要，
    保持原尺寸能让游戏侧的布局逻辑完全不受影响。
    """
    import UnityPy

    game = ctx.game
    src = _unity_path(game)
    bak = backup_dir() / BACKUP_NAME
    base = runtime.bundle_root()

    if not bak.is_file():
        shutil.copy2(src, bak)

    env = UnityPy.load(str(src))
    done = {"frames": 0, "masks": 0, "missing": []}
    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        d = o.read()
        nm = d.m_Name or ""
        if nm in FRAME_MAP:
            rd = Image.open(base / FRAME_DIR / f"{FRAME_MAP[nm]}.png").convert("RGBA")
            rd = rd.resize((d.m_Width, d.m_Height), Image.LANCZOS)
            d.image = rd
            d.save()
            done["frames"] += 1
        elif nm in MASK_MAP:
            p = base / MASK_DIR / MASK_MAP[nm]
            if not p.is_file():
                done["missing"].append(nm)
                continue
            rd = Image.open(p).convert("RGBA")
            if rd.size != (d.m_Width, d.m_Height):
                rd = rd.resize((d.m_Width, d.m_Height), Image.LANCZOS)
            d.image = rd
            d.save()
            done["masks"] += 1

    data = env.file.save()
    src.write_bytes(data)

    from datetime import datetime

    (backup_dir() / MARK_NAME).write_text(json.dumps({
        "applied": True,
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "frames": done["frames"],
        "masks": done["masks"],
    }, ensure_ascii=False), encoding="utf-8")
    return done


def revert_theme(ctx) -> dict:
    """把备份的 data.unity3d 写回去，恢复原版卡框与遮罩。"""
    src = _unity_path(ctx.game)
    bak = backup_dir() / BACKUP_NAME
    if not bak.is_file():
        raise MDStudioError(tr("no backup to restore"))
    shutil.copy2(bak, src)
    p = backup_dir() / MARK_NAME
    if p.is_file():
        p.unlink()
    return {"restored": True}
