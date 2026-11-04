# -*- coding: utf-8 -*-
"""生成 MDStudio 的图标资源.

主图标用角色图(白底贴纸风格): 深炭底 + 角色内嵌.
白底要剥掉 —— 泛洪只删「与边界连通且纯白低饱和」的像素, 白发偏蓝白所以能保住;
用 isnet-anime 模型抠像反而不行, 它分不清背景白和头发白, 会把头发一起吃掉.

角色图来源:
  tools/assets/role_cutout.png   已剥好底的项目内素材(优先用这个)
  $MDSTUDIO_ROLE_SRC 指定的原图, 现场剥(默认 tools/assets/role_src.png)
都没有就退回几何图标.
"""
import os
import struct

import cv2
import numpy as np
from PIL import Image, ImageDraw

# 相对脚本自身推导, 工程目录改名也不影响
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(_ROOT, "src", "MDStudio", "Assets")
TOOLS = os.path.join(_ROOT, "tools")
ROLE_CUTOUT = os.path.join(TOOLS, "assets", "role_cutout.png")
ROLE_SRC = os.environ.get("MDSTUDIO_ROLE_SRC",
                          os.path.join(TOOLS, "assets", "role_src.png"))

BG = (27, 26, 33, 255)        # 深炭黑
DARK_RING = (44, 42, 58, 255)
IVORY = (247, 241, 227, 255)
DIAMOND = (216, 57, 43, 255)

SS = 4          # 超采样倍数, 抗锯齿


def rounded_mask(size, r=0.22):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                         radius=int(size * r), fill=255)
    return m


def strip_white(src, sat_thr=14, val_thr=246, dilate=3):
    """剥掉白底, 只删与边界连通且纯白低饱和的像素

    白发是偏蓝的白(饱和度不为零), 背景是纯白, 靠这个差别分开。
    角色自带的深色描边保留, 那是立绘的一部分。
    """
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).copy()
    h, w = a.shape[:2]
    hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV)
    S, V = hsv[:, :, 1], hsv[:, :, 2]
    cand = ((S < sat_thr) & (V > val_thr)).astype(np.uint8) * 255
    cand = cv2.morphologyEx(cand, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    _n, lab = cv2.connectedComponents(cand, 8)
    border = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    border.discard(0)
    bg = np.zeros((h, w), np.uint8)
    for i in border:
        bg |= (lab == i).astype(np.uint8) * 255
    if dilate:
        bg = cv2.dilate(bg, np.ones((3, 3), np.uint8), iterations=dilate)
    alpha = cv2.GaussianBlur(cv2.bitwise_not(bg), (0, 0), 1.2)
    rgba = im.convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha, "L"))
    return rgba


def drop_islands(rgba, min_area_ratio=0.02):
    """删掉不与主体连通的碎块

    剥掉白底后, 立绘自带的深色描边会变成一撮孤立的黑块留在那里(描边与背景之间
    的白被剥掉了)。只保留跟主体连着的那块, 碎块直接清掉。
    """
    a = np.asarray(rgba.split()[3])
    m = (a > 40).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 2:
        return rgba
    areas = stats[1:, cv2.CC_STAT_AREA]
    main = 1 + int(areas.argmax())
    total = float(a.sum())
    keep = np.zeros_like(lab, dtype=bool)
    for i in range(1, n):
        if i == main:
            keep |= (lab == i)
        elif stats[i, cv2.CC_STAT_AREA] / max(1.0, total / 255.0) >= min_area_ratio:
            keep |= (lab == i)      # 够大的碎块留着
    out = rgba.copy()
    out.putalpha(Image.fromarray((a * keep).astype(np.uint8), "L"))
    return out


# 裁剪框(相对比例): 只取脸和权杖头, 红宝石这个强色块在小尺寸下能认人
ROLE_CROP = (0.08, 0.00, 0.90, 0.76)


# 裁剪框(相对比例): 只取脸和权杖头, 红宝石这个强色块在小尺寸下能认人
ROLE_CROP = (0.08, 0.00, 0.90, 0.76)


def crop_role(role):
    """按 ROLE_CROP 裁掉权杖下段和右下角的黑块, 让主体占满"""
    w, h = role.size
    x0, y0, x1, y1 = ROLE_CROP
    return role.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def load_role():
    """优先用项目里已剥好底的素材, 其次现场剥原图"""
    if os.path.isfile(ROLE_CUTOUT):
        print("用项目内素材:", ROLE_CUTOUT)
        role = Image.open(ROLE_CUTOUT).convert("RGBA")
        return role
    if os.path.isfile(ROLE_SRC):
        print("现场剥原图:", ROLE_SRC)
        role = strip_white(ROLE_SRC)
        role = drop_islands(role)
        os.makedirs(os.path.dirname(ROLE_CUTOUT), exist_ok=True)
        role.save(ROLE_CUTOUT)
        return role
    print("!! 找不到角色图, 退回几何图标:", ROLE_SRC)
    return None


def geo(size):
    """几何图标: 深底 + 橙红菱形 + 米白播放符号"""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG)
    cx = cy = S / 2
    r = S * 0.30
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=DIAMOND)
    r2 = S * 0.225
    d.polygon([(cx, cy - r2), (cx + r2, cy), (cx, cy + r2), (cx - r2, cy)], fill=BG)
    tw, th = S * 0.165, S * 0.195
    x0 = cx - tw * 0.32
    d.polygon([(x0, cy - th / 2), (x0, cy + th / 2), (x0 + tw, cy)], fill=IVORY)
    return img.resize((size, size), Image.LANCZOS)


def plated(role, size, inset=0.98):
    """深底 + 角色图内嵌"""
    if role is None:
        return geo(size)
    out = Image.new("RGBA", (size, size), BG)
    inner = max(1, int(size * inset))
    fg = crop_role(role).resize((inner, inner), Image.LANCZOS)
    off = (size - inner) // 2
    out.paste(fg, (off, off), fg)
    o = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    o.paste(out, (0, 0), rounded_mask(size))
    return o


def stroked(role, size):
    """角色 + 圆角 + 深色描边. 任务栏的小图标用这个, 浅色栏上也不会化掉"""
    if role is None:
        return geo(size)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fg = crop_role(role).resize((size, size), Image.LANCZOS)
    out.paste(fg, (0, 0), fg)
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22),
        outline=DARK_RING, width=max(1, int(size * 0.06)))
    out = Image.alpha_composite(out, ring)
    out.putalpha(Image.composite(out.split()[3], Image.new("L", (size, size), 0),
                                 rounded_mask(size)))
    return out


def banner(role, w, h, ratio=0.62):
    """深底 + 居中角色图"""
    out = Image.new("RGBA", (w, h), BG)
    if role is None:
        ic = geo(int(h * ratio))
    else:
        ic = crop_role(role).resize((int(h * ratio), int(h * ratio)), Image.LANCZOS)
    out.paste(ic, ((w - ic.width) // 2, (h - ic.height) // 2), ic)
    return out


def save_png(name, img):
    p = os.path.join(ASSETS, name)
    img.save(p, "PNG")
    print("写出", name, img.size)


def make_bmp_ico(path, images):
    """手写纯 BMP 格式的 ico

    PIL 存的 ico 用 PNG 压缩帧, MSBuild 的资源编译器不认, exe 里就没了图标资源.
    Win32 只认 BMP 帧: BITMAPINFOHEADER + 自下而上的 BGRA 像素 + 1bpp AND 掩码.
    images: [(size, RGBA Image)]
    """
    frames = []
    for size, im in images:
        im = im.convert("RGBA")
        if im.width != size:
            im = im.resize((size, size), Image.LANCZOS)
        px = im.load()
        body = bytearray()
        for y in range(size - 1, -1, -1):
            for x in range(size):
                r, g, b, a = px[x, y]
                body += bytes((b, g, r, a))
        mask_stride = ((size + 31) // 32) * 4
        mask = bytearray()
        for y in range(size - 1, -1, -1):
            row = bytearray(mask_stride)
            for x in range(size):
                if px[x, y][3] == 0:
                    row[x // 8] |= 0x80 >> (x % 8)
            mask += row
        dib = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                          len(body) + len(mask), 0, 0, 0, 0)
        frames.append(dib + bytes(body) + bytes(mask))

    out = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    for size, _im in images:
        data = frames[len([x for x in out[6:]]) if False else 0] if False else None
    offset = 6 + 16 * len(frames)
    for (size, _im), data in zip(images, frames):
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                           len(data), offset)
        offset += len(data)
    for data in frames:
        out += data
    with open(path, "wb") as f:
        f.write(bytes(out))
    print("写出 BMP 格式 ico:", os.path.basename(path), os.path.getsize(path), "字节")


def save_ico(name, img):
    p = os.path.join(ASSETS, name)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    make_bmp_ico(p, [(s, img.resize((s, s), Image.LANCZOS)) for s in sizes])
    print("写出", name, os.path.getsize(p), "字节")


def main():
    os.makedirs(ASSETS, exist_ok=True)
    role = load_role()

    save_ico("AppIcon.ico", plated(role, 256))
    save_png("AppIcon.png", plated(role, 64))
    save_png("Square44x44Logo.scale-200.png", plated(role, 88))
    save_png("Square150x150Logo.scale-200.png", plated(role, 300))
    save_png("StoreLogo.png", plated(role, 50))
    save_png("LockScreenLogo.scale-200.png", plated(role, 48))
    # 任务栏上的小图标不带底板, 用描边版
    save_png("Square44x44Logo.targetsize-24_altform-unplated.png", stroked(role, 24))
    save_png("Square44x44Logo.targetsize-48_altform-lightunplated.png", stroked(role, 48))
    save_png("Wide310x150Logo.scale-200.png", banner(role, 620, 300))
    save_png("SplashScreen.scale-200.png", banner(role, 1240, 600, ratio=0.45))
    print("全部完成")


if __name__ == "__main__":
    main()
