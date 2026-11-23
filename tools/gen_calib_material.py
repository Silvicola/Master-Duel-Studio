# -*- coding: utf-8 -*-
"""生成校准比例用的 60 帧标定素材。

用法：python tools/gen_calib_material.py [--out 目录] [--frames 60] [--size 1024]

产出：000.png … 059.png、sheet.png、说明.txt

为什么长这样：
- 帧必须是正方形。生成管线会把所有帧裁成并集包围盒的正方形（frames._crop_union），
  正方形时「帧宽 = 帧高 = 尺寸」，量高度才和代码里的换算一一对应。
- 上下两条彩色端帽贴着画布边缘，游戏里量端帽之间的高度就是方框高度，不会看错边。
- 每 10% 一道刻度，中间印大号帧号，录屏暂停能直接读帧号。
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

RED = (196, 43, 28)
BLUE = (0, 120, 212)
INK = (40, 40, 40)


def font(size: int, bold: bool = False):
    for name in (("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "msyhbd.ttc")) + ("segoeui.ttf",):
        p = Path("C:/Windows/Fonts") / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                pass
    return ImageFont.load_default()


def one_frame(i: int, n: int, side: int) -> Image.Image:
    im = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # 竖柱靠右，左边留白给帧号与刻度标签，互不遮挡
    col_w = round(side * 0.26)
    x0 = round(side * 0.62)
    x1 = x0 + col_w

    d.rectangle([x0, 0, x1 - 1, side - 1], fill=(244, 244, 244, 255),
                outline=INK, width=max(2, side // 340))

    cap = round(side * 0.04)
    d.rectangle([x0, 0, x1 - 1, cap], fill=RED)
    d.rectangle([x0, side - cap - 1, x1 - 1, side - 1], fill=BLUE)

    # 每 10% 一道刻度：从柱子左边往左伸，标签再往左
    tick_font = font(max(18, side // 30))
    tick_len = round(side * 0.10)
    for k in range(1, 10):
        y = round(side * k / 10)
        d.line([(x0 - tick_len, y), (x0, y)], fill=INK, width=max(2, side // 300))
        d.text((x0 - tick_len - max(8, side // 60), y), f"{k * 10}%", font=tick_font,
               fill=(90, 90, 90, 255), anchor="rm")

    # 大号帧号，落在左侧空白区，缩到不碰刻度标签
    big = font(round(side * 0.18), bold=True)
    d.text((round(side * 0.22), side // 2), f"{i:03d}", font=big,
           fill=(24, 95, 165, 255), anchor="mm")

    d.text((x0 + 8, cap + 6), f"{i + 1}/{n}", font=tick_font, fill=(70, 70, 70, 255))
    return im


def sheet(frames, side: int, out: Path):
    cell, gap, cols = 150, 6, 10
    rows = (len(frames) + cols - 1) // cols
    head = 60
    w = cols * cell + (cols + 1) * gap
    h = head + rows * cell + (rows + 1) * gap
    s = Image.new("RGB", (w, h), (247, 247, 247))
    d = ImageDraw.Draw(s)
    f18, f16 = font(18), font(16)
    d.text((gap + 2, 12), f"标定素材 · {len(frames)} 帧 · 正方形 {side}×{side}", font=f18, fill=(40, 40, 40))
    d.text((gap + 2, 34), "格内数字是帧号，红帽在上、蓝帽在下", font=f16, fill=(120, 120, 120))
    for i, im in enumerate(frames):
        r, c = divmod(i, cols)
        x = gap + c * (cell + gap)
        y = head + gap + r * (cell + gap)
        d.rectangle([x, y, x + cell, y + cell], fill=(255, 255, 255), outline=(226, 226, 226))
        th = im.copy()
        th.thumbnail((cell - 8, cell - 8), Image.LANCZOS)
        s.paste(th, (x + (cell - th.width) // 2, y + (cell - th.height) // 2), th)
        d.text((x + 5, y + cell - 18), f"{i:03d}", font=f16, fill=(60, 60, 60))
    s.save(out / "sheet.png", "PNG")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--size", type=int, default=1024)
    a = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    out = Path(a.out) if a.out else (root.parent / "calib" / "标定素材-60帧")
    out.mkdir(parents=True, exist_ok=True)

    ims = []
    for i in range(a.frames):
        im = one_frame(i, a.frames, a.size)
        im.save(out / f"{i:03d}.png", "PNG")
        ims.append(im)
    sheet(ims, a.size, out)

    ratio = 1.0 / 1.04            # 生成时并集包围盒会外扩 4%
    region = 2930 / 5000         # 尺寸 2930 在画布高度里的占比
    note = f"""标定素材 · {a.frames} 帧 · {a.size}×{a.size} 正方形
使用步骤：
1. 软件里导入这个文件夹（帧文件夹导入），总帧数 {a.frames}、时长 2 秒、尺寸 2930、放大 1、其余默认、弹出效果「默认」。
2. 生成后不要复原，进游戏录屏：整屏 16:9，不要窗口化、不要任何界面缩放。
3. 峰值在 0.64 秒、收尾在 1.36 秒，各停一帧截图。

理论值（尺寸按 2930 算，实际以 meta 里的 size_units 为准）：
- 方框高度占画面：{region*100:.1f}%（缩放 100%）
- 红帽到蓝帽之间的柱子占方框：{ratio*100:.1f}%
- 合起来柱子占画面：{region*ratio*100:.1f}%
- 峰值 108%：{region*ratio*1.08*100:.1f}%
- 收尾 90%：{region*ratio*0.90*100:.1f}%

对比方法：在录像里量红帽上沿到蓝帽下沿的像素，除以视频高度，与上面最后一行的数比。
差多少，预览里的 UnitsToPixels 就按 5000 × 对比值 / 实测值 修正。
"""
    (out / "说明.txt").write_text(note, encoding="utf-8")
    print(f"输出 {out}：{a.frames} 帧 + sheet.png + 说明.txt")
    print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
