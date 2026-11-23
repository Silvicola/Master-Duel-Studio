# -*- coding: utf-8 -*-
"""从预览缓存出一套校准用的帧图。

用法：
    python tools/calib_frames.py --card 22804
    python tools/calib_frames.py --card 22804 --pack dist/MDStudio-v-xxx --out calib/22804

产出（默认落在工程外的 calib/<卡号>/，dist 会随打包清空，别放里面）：
    frames/000.png …    单帧，编号与软件里第几帧一一对应
    sheet.png           10×6 拼接，每格标帧号
    peak.png            弹到最大的那一帧，铺在 16:9 画布上，带百分比刻度与角色高度标注
    tail.png            末帧，同样的画布与刻度
    说明.txt            理论占比与对比方法

为什么要有 peak/tail：游戏录屏是 16:9 全屏，只有把帧摆回同样比例的画布上，
量出来的「角色高度占画面百分比」才和录像能直接比。
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CANVAS = (1600, 900)          # 16:9，和预览画布同一比例
UNITS_TO_PIXELS = 2880.0      # 与 CutinPreview.UnitsToPixels 保持一致
FIELD_REL = ("Assets", "Preview", "cutin-field.png")


def load_font(size: int):
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        p = Path("C:/Windows/Fonts") / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                pass
    return ImageFont.load_default()


def find_cache(pack: Path, card: int) -> Path:
    hits = sorted(pack.glob(f"data/*/*/preview/{card}"))
    if not hits:
        raise SystemExit(f"{pack} 下没有 {card} 的预览缓存。先在软件里生成一次，别点复原。")
    return hits[-1]


def scale_at(t: float, duration: float, curve) -> float:
    """曲线是「比例帧号 → 缩放百分比」，和播放器的插值同一套。"""
    if not curve:
        return 1.0
    last = max(pt[0] for pt in curve) or 1
    f = 0 if duration <= 0 else t / duration * last
    if f <= curve[0][0]:
        return curve[0][1] / 100.0
    for i in range(1, len(curve)):
        f0, s0 = curve[i - 1][0], curve[i - 1][1]
        f1, s1 = curve[i][0], curve[i][1]
        if f <= f1:
            span = f1 - f0
            k = 0 if span <= 0 else (f - f0) / span
            return (s0 + (s1 - s0) * k) / 100.0
    return curve[-1][1] / 100.0


def field_layer(pack: Path):
    p = pack.joinpath(*FIELD_REL)
    if not p.is_file():
        return None
    field = Image.open(p).convert("RGB")
    k = max(CANVAS[0] / field.width, CANVAS[1] / field.height)
    field = field.resize((round(field.width * k), round(field.height * k)), Image.LANCZOS)
    left = (field.width - CANVAS[0]) // 2
    top = (field.height - CANVAS[1]) // 2
    field = field.crop((left, top, left + CANVAS[0], top + CANVAS[1]))
    # 播放器把线稿压到 30%：白底上混白 70%
    return Image.blend(field, Image.new("RGB", CANVAS, "white"), 0.70)


def compose(pack, frame: Image.Image, scale: float, size_units: float, zoom: float, title: str, font_big, font_small):
    """把一帧摆到 16:9 画布上，画 10%刻度、角色高度标注。"""
    canvas = Image.new("RGBA", CANVAS, (250, 251, 252, 255))
    bg = field_layer(pack)
    if bg is not None:
        canvas.alpha_composite(bg.convert("RGBA"))

    draw_h = size_units * zoom * (CANVAS[1] / UNITS_TO_PIXELS) * scale
    k = draw_h / max(1, frame.height)
    draw_w = max(1, round(frame.width * k))
    draw_h = max(1, round(draw_h))
    f = frame.resize((draw_w, draw_h), Image.LANCZOS)
    x = (CANVAS[0] - draw_w) // 2
    y = (CANVAS[1] - draw_h) // 2
    canvas.alpha_composite(f, (x, y))

    # 角色实际不透明区域（帧可能带极小边距）
    bbox = f.getbbox()
    if bbox:
        top, bottom = y + bbox[1], y + bbox[3]
    else:
        top, bottom = y, y + draw_h
    height_pct = (bottom - top) / CANVAS[1] * 100

    d = ImageDraw.Draw(canvas, "RGBA")
    for pct in range(10, 100, 10):
        gy = round(CANVAS[1] * pct / 100)
        for gx in range(0, CANVAS[0], 24):
            d.line([(gx, gy), (gx + 12, gy)], fill=(0, 0, 0, 55), width=1)
        d.text((14, gy - 20), f"{pct}%", font=font_small, fill=(90, 90, 90, 220))
    d.line([(CANVAS[0] // 2, 0), (CANVAS[0] // 2, CANVAS[1])], fill=(0, 120, 212, 90), width=1)

    # 角色高度：两端横杠 + 竖线 + 读数
    cx = x + draw_w // 2
    d.line([(cx - 26, top), (cx + 26, top)], fill=(0, 120, 212, 235), width=2)
    d.line([(cx - 26, bottom), (cx + 26, bottom)], fill=(0, 120, 212, 235), width=2)
    d.line([(cx, top), (cx, bottom)], fill=(0, 120, 212, 235), width=2)
    label = f"{height_pct:.1f}%"
    d.text((cx + 34, (top + bottom) // 2 - 14), label, font=font_big, fill=(0, 90, 160, 255))

    d.text((CANVAS[0] - 330, 16), title, font=font_small, fill=(70, 70, 70, 235))
    d.text((16, CANVAS[1] - 34), "画布 1600×900（16:9）", font=font_small, fill=(110, 110, 110, 220))
    return canvas.convert("RGB"), height_pct


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", type=int, required=True)
    ap.add_argument("--pack", default=None, help="便携版目录，默认取 dist 下最新一个")
    ap.add_argument("--out", default=None, help="输出目录，默认 calib/<卡号>")
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    pack = Path(args.pack) if args.pack else None
    if pack is None:
        cands = sorted((root / "dist").glob("MDStudio-v-*"), key=lambda p: p.name)
        cands = [p for p in cands if p.is_dir()]
        if not cands:
            raise SystemExit("dist 下没有便携版目录，用 --pack 指定")
        pack = cands[-1]

    cache = find_cache(pack, args.card)
    meta = json.loads((cache / "meta.json").read_text(encoding="utf-8"))
    count = int(meta.get("frames") or 0)
    duration = float(meta.get("duration") or 0)
    size_units = float(meta.get("size_units") or 2880)
    zoom = float(meta.get("zoom") or 1.0)
    curve = meta.get("curve") or []
    if count <= 0:
        raise SystemExit("缓存是空的")

    out = Path(args.out) if args.out else (root.parent / "calib" / str(args.card))
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for i in range(count):
        p = cache / f"{i:03d}.webp"
        if not p.is_file():
            break
        im = Image.open(p).convert("RGBA")
        frames.append(im)
        im.save(frames_dir / f"{i:03d}.png", "PNG")
    if not frames:
        raise SystemExit("一张帧都没读到")

    font_big = load_font(30)
    font_small = load_font(18)

    # 拼接：10 列，格内等比缩放，左下角标帧号
    cell, gap, cols = 150, 6, 10
    rows = (len(frames) + cols - 1) // cols
    head = 64
    w = cols * cell + (cols + 1) * gap
    h = head + rows * cell + (rows + 1) * gap
    sheet = Image.new("RGB", (w, h), (247, 247, 247))
    d = ImageDraw.Draw(sheet)
    d.text((gap + 2, 14),
           f"卡 {args.card} · {count} 帧 · {duration:.2f} 秒 · 尺寸 {size_units:.0f}",
           font=font_small, fill=(50, 50, 50))
    d.text((gap + 2, 36), "格内数字是帧号，与软件里第几帧一一对应", font=font_small, fill=(120, 120, 120))
    for i, im in enumerate(frames):
        r, c = divmod(i, cols)
        x = gap + c * (cell + gap)
        y = head + gap + r * (cell + gap)
        d.rectangle([x, y, x + cell, y + cell], fill=(255, 255, 255), outline=(225, 225, 225))
        thumb = im.copy()
        thumb.thumbnail((cell - 8, cell - 8), Image.LANCZOS)
        sheet.paste(thumb, (x + (cell - thumb.width) // 2, y + (cell - thumb.height) // 2), thumb)
        d.text((x + 5, y + cell - 18), f"{i:03d}", font=font_small, fill=(60, 60, 60))
    sheet.save(out / "sheet.png", "PNG")

    peak_i = max(range(len(frames)),
                 key=lambda i: scale_at(duration * i / max(1, len(frames) - 1), duration, curve))
    tail_i = len(frames) - 1
    peak_scale = scale_at(duration * peak_i / max(1, len(frames) - 1), duration, curve)
    tail_scale = scale_at(duration, duration, curve)

    peak_img, peak_pct = compose(pack, frames[peak_i], peak_scale, size_units, zoom,
                                 f"峰值帧 {peak_i:03d} · 缩放 {peak_scale*100:.0f}%", font_big, font_small)
    tail_img, tail_pct = compose(pack, frames[tail_i], tail_scale, size_units, zoom,
                                 f"末帧 {tail_i:03d} · 缩放 {tail_scale*100:.0f}%", font_big, font_small)
    peak_img.save(out / "peak.png", "PNG")
    tail_img.save(out / "tail.png", "PNG")

    peak_t = duration * peak_i / max(1, len(frames) - 1)
    note = (
        f"卡号 {args.card}\n"
        f"帧数 {count}，时长 {duration:.2f} 秒，尺寸 {size_units:.0f}，共 {len(frames)} 张单帧\n\n"
        f"峰值帧 {peak_i:03d}（约 {peak_t:.2f} 秒，缩放 {peak_scale*100:.0f}%），角色高 {peak_pct:.1f}% 画面\n"
        f"末帧 {tail_i:03d}（约 {duration:.2f} 秒，缩放 {tail_scale*100:.0f}%），角色高 {tail_pct:.1f}% 画面\n\n"
        "对比方法：\n"
        "1. 游戏里整屏 16:9 录制，不要窗口化、不要界面缩放。\n"
        "2. 在录像里停到 0.64 秒附近的峰值帧，量角色头顶到脚底的像素，除以视频高度。\n"
        "3. 和上面的峰值百分比比，差多少，UnitsToPixels 就按 5000×对比值/实测值 修正。\n"
    )
    (out / "说明.txt").write_text(note, encoding="utf-8")

    print(f"输出目录 {out}")
    print(f"  frames/  {len(frames)} 张")
    print(f"  sheet.png  {sheet.width}×{sheet.height}")
    print(f"  peak.png  帧 {peak_i:03d}  角色高 {peak_pct:.1f}%")
    print(f"  tail.png  帧 {tail_i:03d}  角色高 {tail_pct:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
