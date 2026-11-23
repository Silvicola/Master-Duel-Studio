# -*- coding: utf-8 -*-
"""生成特效比例标定用的 60 帧素材（16:9 满幅）。

用法：python tools/gen_calib_runfx.py [--out 目录] [--frames 60] [--width 1280]

产出：000.png … 059.png、sheet.png、说明.txt

为什么长这样：
- 特效的帧会被当成整块粒子贴图铺出去，所以画面必须**满幅、不透明**：
  外圈一圈白框正好压在帧边上，游戏里量这块亮矩形的四边就能算出它占屏幕多大。
- 10% 网格让画面内部也有参照，量到边缘被裁掉的那一档时还能按格子推算。
- 大号帧号放在画面中间偏上，暂停录屏能直接读出第几帧，方便和音效对齐。
- 四角画短斜线，用来确认图片有没有被旋转或镜像。
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (14, 14, 16)
EDGE = (255, 255, 255)
GRID = (58, 58, 64)
MARK = (255, 70, 70)


def font(size: int, bold: bool = False):
    names = ("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "msyhbd.ttc")
    for name in names + ("segoeui.ttf",):
        p = Path("C:/Windows/Fonts") / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                pass
    return ImageFont.load_default()


def one_frame(i: int, n: int, w: int, h: int) -> Image.Image:
    im = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(im)

    # 外框：贴边画，白框的外沿就是这张贴图的边界，量的就是特效的实际显示框
    edge = max(4, round(h * 0.008))
    d.rectangle([0, 0, w - 1, h - 1], outline=EDGE, width=edge)

    # 10% 网格
    for k in range(1, 10):
        x = round(w * k / 10)
        d.line([(x, 0), (x, h - 1)], fill=GRID, width=max(2, round(h * 0.003)))
        y = round(h * k / 10)
        d.line([(0, y), (w - 1, y)], fill=GRID, width=max(2, round(h * 0.003)))

    # 中心十字：量中心有没有偏移
    cx, cy = w // 2, h // 2
    arm = round(h * 0.05)
    d.line([(cx - arm, cy), (cx + arm, cy)], fill=MARK, width=max(3, round(h * 0.006)))
    d.line([(cx, cy - arm), (cx, cy + arm)], fill=MARK, width=max(3, round(h * 0.006)))

    # 四角斜线：确认没被旋转/镜像
    tick = round(h * 0.05)
    for (x0, y0, dx, dy) in ((edge, edge, 1, 1), (w - edge, edge, -1, 1),
                             (edge, h - edge, 1, -1), (w - edge, h - edge, -1, -1)):
        d.line([(x0, y0), (x0 + dx * tick, y0 + dy * tick)],
               fill=EDGE, width=max(3, round(h * 0.006)))

    # 大号帧号：放上半区，别压到中心十字（不然量中心会被数字挡住）
    big = font(round(h * 0.24), bold=True)
    d.text((cx, round(h * 0.27)), f"{i:03d}", font=big, fill=EDGE, anchor="mm")

    small = font(max(18, round(h * 0.045)))
    d.text((round(w * 0.04), round(h * 0.10)), f"{i + 1}/{n}", font=small, fill=MARK, anchor="lm")
    d.text((round(w * 0.04), round(h * 0.90)), f"{w}x{h}", font=small, fill=MARK, anchor="lm")
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="calib/特效标定-60帧")
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--width", type=int, default=1280)
    # 给 0 就出正方形：用来分辨「画面大小」按屏宽还是按屏高
    ap.add_argument("--height", type=int, default=0)
    a = ap.parse_args()

    w = a.width
    h = a.height if a.height > 0 else round(w * 9 / 16)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    frames = []
    for i in range(a.frames):
        im = one_frame(i, a.frames, w, h)
        im.save(out / f"{i:03d}.png")
        frames.append(im)

    cols = 10
    tw = w // 4
    th = h // 4
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * (th + 18)), (245, 245, 245))
    d = ImageDraw.Draw(sheet)
    f = font(13)
    for n, im in enumerate(frames):
        x = (n % cols) * tw
        y = (n // cols) * (th + 18)
        sheet.paste(im.resize((tw, th), Image.LANCZOS), (x, y + 18))
        d.text((x + 4, y + 2), f"{n:03d}", font=f, fill=(30, 30, 30))
    sheet.save(out / "sheet.png")

    (out / "说明.txt").write_text(
        "特效比例标定素材\n"
        "================\n\n"
        f"{a.frames} 张 {w}x{h}（16:9）、不透明、满幅。\n\n"
        "画面元素\n"
        "- 四周白框贴边：游戏里量这块亮矩形的四边，就是特效实际占屏幕的范围。\n"
        "- 10% 网格：边缘被裁掉时按格子推算。\n"
        "- 中心红十字：量中心有没有偏。\n"
        "- 大号帧号：暂停后读第几帧，用来和音效对齐。\n\n"
        "怎么用\n"
        "1. 特效页选「视频 / GIF / 帧文件夹」，把本文件夹拖进去。\n"
        "2. 帧数 60、时长 2、像素/帧默认、画面大小 0.5，其余默认，模式「全屏」。\n"
        "3. 生成后进游戏触发特效，整屏 16:9 录 4 秒，游戏音效开着。\n"
        "4. 复原，画面大小改 1.0 再生成一次，再录一段。\n"
        "5. 两段视频发出，第二次先别复原。\n",
        encoding="utf-8")
    print(f"写出 {a.frames} 帧 + sheet.png + 说明.txt 到 {out}")


if __name__ == "__main__":
    main()
