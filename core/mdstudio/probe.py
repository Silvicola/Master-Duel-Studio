"""导入素材的合法性检查。

对话框的扩展名过滤拦不住人：在文件名框里粘贴完整路径时系统对话框不看过滤器，
把 .mp4 改名成 .png 也照样能选。所以内容层面的判断放在这里，
界面在选完文件后立刻问一句，后端正式流程里也会再问一次。

判断只读文件头，不解码、不依赖 ffmpeg / opencv，毫秒级返回。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

# 视频容器的魔数。MP4/MOV 的第 4..8 字节是 "ftyp"，MKV/WebM 是 EBML 头。
_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".flv"}
_IMAGE_EXT = {".png", ".webp", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"}


def sniff(path) -> str:
    """按内容判断这是什么：image / video / audio / zip / unity / unknown。"""
    p = Path(path)
    try:
        with p.open("rb") as f:
            head = f.read(16)
    except OSError:
        return "unknown"
    if not head:
        return "unknown"

    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith(b"GIF8"):
        return "image"
    if head.startswith(b"BM"):
        return "image"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "image"
    if head[:4] == b"RIFF":
        form = head[8:12]
        if form == b"WEBP":
            return "image"
        if form == b"WAVE":
            return "audio"
        return "unknown"
    if head[4:8] == b"ftyp":
        return "video"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"UnityFS") or head.startswith(b"UnityWeb"):
        return "unity"
    return "unknown"


def _image_ok(path: Path) -> tuple[bool, str]:
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.verify()          # 只校验结构，不解码整张图
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _spine_dir_ok(d: Path) -> tuple[bool, str]:
    """Spine 导出目录/压缩包要有骨架 json 和 atlas，缺一样导入必然失败。"""
    files = list(d.rglob("*")) if d.is_dir() else []
    names = {f.name.lower() for f in files if f.is_file()}
    has_json = any(n.endswith(".json") for n in names)
    has_atlas = any(n.endswith(".atlas") for n in names)
    if has_json and has_atlas:
        return True, ""
    if not has_json and not has_atlas:
        return False, "文件夹里没有 Spine 的 .json 骨架和 .atlas 图集"
    if not has_json:
        return False, "文件夹里没有 Spine 的 .json 骨架"
    return False, "文件夹里没有 .atlas 图集"


def _spine_zip_ok(p: Path) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(p) as z:
            names = [n.lower() for n in z.namelist()]
    except Exception as e:
        return False, f"压缩包打不开：{e}"
    has_json = any(n.endswith(".json") for n in names)
    has_atlas = any(n.endswith(".atlas") for n in names)
    if has_json and has_atlas:
        return True, ""
    if not has_json and not has_atlas:
        return False, "压缩包里没有 Spine 的 .json 骨架和 .atlas 图集"
    if not has_json:
        return False, "压缩包里没有 Spine 的 .json 骨架"
    return False, "压缩包里没有 .atlas 图集"


def _frames_ok(d: Path) -> tuple[bool, str]:
    from PIL import Image

    pics = sorted(q for q in d.iterdir() if q.is_file() and q.suffix.lower() in _IMAGE_EXT)
    if not pics:
        return False, "文件夹里没有图片"
    sizes = set()
    bad = []
    for q in pics[:200]:                      # 只抽查前 200 帧，几千帧全读太慢
        try:
            with Image.open(q) as im:
                sizes.add(im.size)
        except Exception:
            bad.append(q.name)
    if bad:
        head = "、".join(bad[:3])
        tail = f" 等 {len(bad)} 张" if len(bad) > 3 else ""
        return False, f"这些文件不是图片：{head}{tail}"
    if len(sizes) > 1:
        desc = "、".join(f"{w}×{h}" for w, h in sorted(sizes)[:4])
        return False, f"帧尺寸不一致（{desc}），要统一成一个尺寸再导入"
    return True, ""


def check(path, want: str) -> dict:
    """检查这个路径能不能当 want 用。

    want: image / video / frames / spine / pack / game_dir
    返回 {ok, reason, hint}。reason 是一句话原因，hint 是该怎么改（可选）。
    """
    p = Path(str(path)) if path else Path("")
    if not str(path or "").strip() or not p.exists():
        return {"ok": False, "reason": "找不到这个路径", "hint": ""}

    if want == "game_dir":
        return _check_game_dir(p)

    if want == "pack":
        return _check_pack(p)

    if want == "spine":
        kind = sniff(p) if p.is_file() else "dir"
        if p.is_file() and kind == "zip":
            ok, why = _spine_zip_ok(p)
        elif p.is_file() and kind == "unity":
            ok, why = True, ""            # bundle 里的内容交给 probe_version 判
        elif p.is_dir():
            ok, why = _spine_dir_ok(p)
        else:
            return {"ok": False,
                    "reason": "这不是 Spine 导出文件夹、zip 或 bundle",
                    "hint": "在 Spine 里导出时选「JSON」格式，会有 .json 和 .atlas 两个文件。"}
        if not ok:
            return {"ok": False, "reason": why,
                    "hint": "在 Spine 里导出时选「JSON」格式，会有 .json 和 .atlas 两个文件。"}
        return {"ok": True, "reason": "", "hint": ""}

    if want == "frames":
        if not p.is_dir():
            return {"ok": False, "reason": "这里要选一个文件夹，里面放按顺序编号的图片",
                    "hint": "视频或单张图片请用「视频/GIF」入口。"}
        ok, why = _frames_ok(p)
        if not ok:
            return {"ok": False, "reason": why,
                    "hint": "帧文件夹里只放图片（PNG、JPG、WebP），尺寸统一。"}
        return {"ok": True, "reason": "", "hint": ""}

    kind = sniff(p)

    if want == "image":
        if kind == "video":
            return {"ok": False,
                    "reason": "这是视频，不能当图片用",
                    "hint": "视频素材请到「动画与特效」页，用那里的「视频/GIF」入口。"}
        if kind == "audio":
            return {"ok": False,
                    "reason": "这是音频，不能当图片用",
                    "hint": "图片要 PNG、JPG 或 WebP。"}
        if kind != "image":
            return {"ok": False,
                    "reason": "这不是图片文件",
                    "hint": "支持 PNG、JPG、WebP、BMP、GIF。"}
        ok, why = _image_ok(p)
        if not ok:
            return {"ok": False, "reason": f"图片打不开（{why}）",
                    "hint": "文件可能下载不完整，重新保存一次再试。"}
        return {"ok": True, "reason": "", "hint": ""}

    if want == "video":
        if kind == "image":
            if p.suffix.lower() == ".gif":
                return {"ok": True, "reason": "", "hint": ""}
            return {"ok": False,
                    "reason": "这是一张静态图片，不是视频",
                    "hint": "静态图要自己做动画，请用旁边的「文件夹」入口。"}
        if kind in ("zip", "unity"):
            return {"ok": False,
                    "reason": "这是压缩包或游戏资源包，不是视频",
                    "hint": "Spine 素材请用下方 Spine 那一行的「文件夹…」或「文件…」。"}
        if kind != "video":
            return {"ok": False,
                    "reason": "这不是视频文件",
                    "hint": "支持 MP4、MOV、MKV、WebM、GIF。"}
        return {"ok": True, "reason": "", "hint": ""}

    return {"ok": True, "reason": "", "hint": ""}


def _check_game_dir(p: Path) -> dict:
    """游戏安装目录要有主数据目录和 LocalData，缺一个后面全都会报错。"""
    if not p.is_dir():
        return {"ok": False, "reason": "这里不是文件夹", "hint": ""}
    names = {q.name.lower() for q in p.iterdir() if q.is_dir()}
    has_data = any(n.endswith("_data") for n in names)
    has_local = "localdata" in names
    if has_data and has_local:
        return {"ok": True, "reason": "", "hint": ""}
    if not has_data and not has_local:
        return {"ok": False,
                "reason": "这个文件夹里既没有游戏的数据目录，也没有 LocalData",
                "hint": "请选 Yu-Gi-Oh! Master Duel 的安装根目录，就是 Steam 里「浏览本地文件」打开的那一层。"}
    if not has_data:
        return {"ok": False,
                "reason": "这个文件夹里有 LocalData，但缺游戏的数据目录",
                "hint": "可能选到了存档子目录，往上一层才是安装根目录。"}
    return {"ok": False,
            "reason": "这个文件夹里有游戏数据，但缺 LocalData",
            "hint": "LocalData 是游戏首次启动后生成的，先启动一次游戏再选。"}


def _check_pack(p: Path) -> dict:
    """整合包：必须是 zip、里面有 pack.json、格式版本对得上。

    只读 zip 目录和那一个 json，不解压内容，几十 MB 的包也是毫秒级。
    """
    import json

    if not p.is_file():
        return {"ok": False, "reason": "整合包要选一个压缩包文件", "hint": ""}
    if sniff(p) != "zip":
        return {"ok": False, "reason": "这不是压缩包",
                "hint": "整合包是本工具导出的 .zip 文件。"}
    try:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            if "pack.json" not in names:
                return {"ok": False,
                        "reason": "这个压缩包里没有 pack.json",
                        "hint": "它可能不是本工具导出的整合包，或者压缩时把内容套了一层文件夹。"}
            meta = json.loads(z.read("pack.json"))
    except zipfile.BadZipFile:
        return {"ok": False, "reason": "压缩包损坏，读不出内容",
                "hint": "重新下载或重新导出一份。"}
    except Exception as e:
        return {"ok": False, "reason": f"读不了这个压缩包：{e}", "hint": ""}

    if not isinstance(meta, dict) or not isinstance(meta.get("cards"), list):
        return {"ok": False, "reason": "pack.json 结构不对",
                "hint": "可能来自别的工具，或者文件已经损坏。"}
    # 版本号只认 pack.FORMAT 一处。这里曾经写死 2，FORMAT 改成 1 之后
    # 预检把自家导出的包全拦了（2026-11-12 用户实测）。
    from mdstudio.ops import pack as P

    fmt = meta.get("format")
    if fmt != P.FORMAT:
        return {"ok": False,
                "reason": f"这个包的格式版本是 {fmt}，本软件只认版本 {P.FORMAT}",
                "hint": "包的内部结构变了，需要新一点的软件才能装。"}
    if not meta["cards"]:
        return {"ok": False, "reason": "这个包里没有任何卡", "hint": ""}
    return {"ok": True, "reason": "", "hint": ""}
