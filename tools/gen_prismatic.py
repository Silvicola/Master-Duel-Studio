"""按卡型调色板生成「棱彩」卡框（移植 超框1/Floowan 的 OfGradientBorderComposer）。

只重上色最外 28px 的金属边框带，调色板按卡型给不同色相（怪兽橙金、魔法青绿、
陷阱品红、融合紫、同调银、超量深蓝、连接青、通常金），天然贴合不同卡片。
其余像素（插图窗、效果框米色、金色画框）保持原样。

输入 solid/card_frameNN.png，输出 prismatic/card_frameNN.png。
"""

import os
import numpy as np
from PIL import Image

RIM = 28            # 最外金属带宽度（超框1 实测 26~28px）
SHAFT = 28          # 左右光轴宽度
CORNER_R = 56.0     # 四角辉光半径
VAL_LIFT = 1.22     # 完成后亮度提升
MIX_FLOOR = 0.58    # 带内最小替换比例，避免深色金属透出来发闷
VIS_ALPHA = 8

# 每个卡型的调色板：(mid RGB, 7 个色相角度, 是否叠全息网格)
# S/V 由 EffectStyleStops 固定，只有色相按卡型变。
SV = [(0.28, 0.90), (0.38, 0.95), (0.35, 0.92), (0.28, 0.93),
      (0.25, 0.90), (0.42, 0.96), (0.28, 0.92)]

PALETTES = {
    "effect":   ((95, 100, 130), (210, 190, 175, 40, 220, 185, 320), False),
    "spell":    ((70, 120, 125), (165, 150, 175, 45, 185, 155, 320), True),
    "ritual":   ((80, 95, 130),  (215, 200, 185, 40, 225, 195, 310), False),
    "trap":     ((115, 75, 120), (310, 290, 330, 40, 280, 320, 200), False),
    "fusion":   ((105, 80, 130), (280, 265, 295, 40, 250, 290, 200), False),
    "synchro":  ((120, 125, 140),(210, 200, 45, 280, 180, 0, 220), False),
    "xyz":      ((85, 90, 120),  (210, 195, 180, 40, 230, 185, 280), False),
    "link":     ((75, 100, 125), (185, 170, 200, 45, 210, 155, 320), True),
    "normal":   ((130, 115, 90), (45, 35, 55, 200, 25, 50, 320), False),
}

REPLACE_BOOST, SHAFT_BOOST, CORNER_BOOST, SHIMMER = 1.15, 1.25, 1.10, 0.88

# PNG 编号 -> 调色板名（PNG 编号与游戏资产 card_frameNN 一一对应）
FRAME_PALETTE = {
    0: "normal", 1: "effect", 2: "ritual", 3: "fusion", 7: "spell",
    8: "trap", 9: "trap", 10: "synchro", 12: "xyz", 13: "normal",
    14: "effect", 15: "xyz", 16: "synchro", 17: "fusion", 18: "link",
    19: "ritual",
}


def _rgb2hsv(r, g, b):
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    v = mx
    d = mx - mn
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    mask = d > 1e-6
    rc = np.where(mask, (mx - r) / np.maximum(d, 1e-6), 0.0)
    gc = np.where(mask, (mx - g) / np.maximum(d, 1e-6), 0.0)
    bc = np.where(mask, (mx - b) / np.maximum(d, 1e-6), 0.0)
    h = np.where(mx == r, bc - gc, np.where(mx == g, 2 + rc - bc, 4 + gc - rc))
    h = (h / 6.0) % 1.0
    return h, s, v


def _hsv2rgb(h, s, v):
    h = (h % 1.0) * 6.0
    i = np.floor(h).astype(int) % 6
    f = h - np.floor(h)
    p = v * (1 - s); q = v * (1 - f * s); t = v * (1 - (1 - f) * s)
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return r, g, b


def _boost_chroma(r, g, b, sat_mul, val_mul):
    h, s, v = _rgb2hsv(r, g, b)
    s = np.clip(s * sat_mul, 0, 1)
    v = np.clip(v * val_mul, 0, 1)
    s = np.where(s < 0.10, 0.10, s)
    return _hsv2rgb(h, s, v)


def _hash2(x, y):
    n = (x.astype(np.int64) * 374761393 + y.astype(np.int64) * 668265263).astype(np.uint32)
    n = ((n ^ (n >> 13)) * np.uint32(1274126177)).astype(np.uint32)
    n = n ^ (n >> 16)
    return (n & np.uint32(0xFFFF)).astype(np.float64) / 65535.0


def _mottle(x, y):
    return _hash2(x, y) * 0.55 + _hash2(x // 3, y // 3) * 0.30 + _hash2(x // 7, y // 7) * 0.15


def _ease_out(t):
    t = np.clip(t, 0, 1)
    u = 1 - t
    return 1 - u * u


def _lerp_hue(a, b, t):
    d = b - a
    d = d - 360.0 * np.round(d / 360.0)
    return (a + d * t) % 360.0


def _sample_stops(stops_h, t01):
    """stops_h: ndarray(7,) 角度; t01: ndarray(N,) -> rgb 0..1 三通道"""
    n = len(stops_h)
    scaled = (t01 % 1.0) * n
    i0 = np.floor(scaled).astype(int) % n
    i1 = (i0 + 1) % n
    local = scaled - np.floor(scaled)
    local = local * local * (3 - 2 * local)  # smoothstep
    s_arr = np.array([v[0] for v in SV])
    v_arr = np.array([v[1] for v in SV])
    h = _lerp_hue(stops_h[i0], stops_h[i1], local)
    s = s_arr[i0] + (s_arr[i1] - s_arr[i0]) * local
    v = v_arr[i0] + (v_arr[i1] - v_arr[i0]) * local
    return _hsv2rgb(h / 360.0, s, v)


def _holo_grid(r, g, b, x, y, margin_t, angle01):
    cell = 8
    on_v = (x % cell) == 0
    on_h = (y % cell) == 0
    if not (on_v.any() or on_h.any()):
        return r, g, b
    grid_stops = np.array([175.0, 330.0, 50.0, 200.0])
    # 网格用固定 4 色相，S/V 单独给
    n = len(grid_stops)
    scaled = (angle01 % 1.0) * n
    i0 = np.floor(scaled).astype(int) % n
    i1 = (i0 + 1) % n
    local = scaled - np.floor(scaled)
    local = local * local * (3 - 2 * local)
    hh = _lerp_hue(grid_stops[i0], grid_stops[i1], local)
    ss = np.full_like(hh, 0.28); vv = np.full_like(hh, 0.97)
    gr, gg, gb = _hsv2rgb(hh / 360.0, ss, vv)
    strength = (0.22 + 0.40 * margin_t) * np.where(on_v & on_h, 1.0, 0.70)
    r = r + (gr - r) * strength
    g = g + (gg - g) * strength
    b = b + (gb - b) * strength
    return r, g, b


def gen(solid_path, out_path, palette_name):
    mid_rgb, hues, use_grid = PALETTES[palette_name]
    im = Image.open(solid_path).convert("RGBA")
    arr = np.asarray(im).astype(np.float64) / 255.0
    h, w = arr.shape[:2]
    alpha = arr[..., 3]

    ys, xs = np.mgrid[0:h, 0:w]
    edge = np.minimum(np.minimum(xs, w - 1 - xs), np.minimum(ys, h - 1 - ys))
    band = (edge < RIM) & (alpha * 255 > VIS_ALPHA)
    yy = ys[band].astype(np.int64)
    xx = xs[band].astype(np.int64)

    edge_d = edge[band].astype(np.float64)
    rim_t = _ease_out(1.0 - np.clip(edge_d / RIM, 0, 1))

    left = 1.0 - np.clip(xx / SHAFT, 0, 1)
    right = 1.0 - np.clip((w - 1 - xx) / SHAFT, 0, 1)
    shaft_t = _ease_out(np.maximum(left, right)) * SHAFT_BOOST

    cx = (w - 1) * 0.5; cy = (h - 1) * 0.5
    def _best(ccx, ccy):
        dx = xx - ccx; dy = yy - ccy
        d = np.sqrt(dx * dx + dy * dy)
        return _ease_out(1.0 - np.clip(d / CORNER_R, 0, 1))
    corner_t = np.maximum(np.maximum(_best(0, 0), _best(w - 1, 0)),
                          np.maximum(_best(0, h - 1), _best(w - 1, h - 1))) * CORNER_BOOST

    band_gate = rim_t
    shaft_t = shaft_t * band_gate
    corner_t = corner_t * band_gate
    margin_t = np.clip(0.40 * rim_t + 0.35 * shaft_t + 0.35 * corner_t, 0, 1)
    mix = np.clip(MIX_FLOOR + (1 - MIX_FLOOR) * margin_t * REPLACE_BOOST, 0, 0.96)

    ang = np.arctan2(yy - cy, xx - cx)
    angle01 = ((ang + np.pi) / (np.pi * 2.0)) % 1.0

    stops_h = np.array(hues, dtype=np.float64)
    sh_r, sh_g, sh_b = _sample_stops(stops_h, angle01)
    mot = _mottle(xx, yy)
    sh2_r, sh2_g, sh2_b = _sample_stops(stops_h, (angle01 + 0.17 + mot * 0.08) % 1.0)
    iri_r = sh_r + (sh2_r - sh_r) * 0.35
    iri_g = sh_g + (sh2_g - sh_g) * 0.35
    iri_b = sh_b + (sh2_b - sh_b) * 0.35

    rim_pull = np.clip(SHIMMER * (0.55 + 0.45 * rim_t), 0, 1)
    mid_r = np.full_like(rim_pull, mid_rgb[0] / 255.0)
    mid_g = np.full_like(rim_pull, mid_rgb[1] / 255.0)
    mid_b = np.full_like(rim_pull, mid_rgb[2] / 255.0)
    tr = mid_r + (iri_r - mid_r) * rim_pull
    tg = mid_g + (iri_g - mid_g) * rim_pull
    tb = mid_b + (iri_b - mid_b) * rim_pull

    # 左右光轴
    st = np.clip(shaft_t * 0.55, 0, 0.75)
    shh_r, shh_g, shh_b = _sample_stops(stops_h, (angle01 + 0.08) % 1.0)
    shh_r, shh_g, shh_b = _boost_chroma(shh_r, shh_g, shh_b, 1.02, 1.14)
    sel = shaft_t > 0.05
    tr = np.where(sel, tr + (shh_r - tr) * st, tr)
    tg = np.where(sel, tg + (shh_g - tg) * st, tg)
    tb = np.where(sel, tb + (shh_b - tb) * st, tb)

    # 四角漏光
    ct = np.clip(corner_t * 0.50, 0, 0.70)
    ch_r, ch_g, ch_b = _sample_stops(stops_h, (angle01 * 0.5 + 0.25) % 1.0)
    ch_r, ch_g, ch_b = _boost_chroma(ch_r, ch_g, ch_b, 1.05, 1.16)
    selc = corner_t > 0.05
    tr = np.where(selc, tr + (ch_r - tr) * ct, tr)
    tg = np.where(selc, tg + (ch_g - tg) * ct, tg)
    tb = np.where(selc, tb + (ch_b - tb) * ct, tb)

    tr, tg, tb = _boost_chroma(tr, tg, tb, 1.0, 1.06 + 0.10 * mot)

    # 与原始像素按 mix 混合
    sr = arr[..., 0][band]; sg = arr[..., 1][band]; sb = arr[..., 2][band]
    out_r = sr + (tr - sr) * mix
    out_g = sg + (tg - sg) * mix
    out_b = sb + (tb - sb) * mix
    out_r, out_g, out_b = _boost_chroma(out_r, out_g, out_b, 0.96, VAL_LIFT)

    if use_grid:
        out_r, out_g, out_b = _holo_grid(out_r, out_g, out_b, xx, yy, margin_t, angle01)

    # 最内 3px 保留原像素：那一段是原生卡框自己的斜面高光，跟着重上色 + 提亮
    # 会变成一道亮蓝白的细环，看着像多了一条白边。恢复原色后过渡才自然。
    back = np.clip((edge_d - (RIM - 3.0)) / 3.0, 0, 1)
    out_r = out_r * (1 - back) + sr * back
    out_g = out_g * (1 - back) + sg * back
    out_b = out_b * (1 - back) + sb * back

    res = np.asarray(im).copy()
    res[..., 0][band] = np.clip(np.round(out_r * 255), 0, 255).astype(np.uint8)
    res[..., 1][band] = np.clip(np.round(out_g * 255), 0, 255).astype(np.uint8)
    res[..., 2][band] = np.clip(np.round(out_b * 255), 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    Image.fromarray(res, "RGBA").save(out_path)
    return out_path


def main():
    src = "tools/overframe/solid"
    dst = "tools/overframe/prismatic"
    for n, pal in sorted(FRAME_PALETTE.items()):
        sp = f"{src}/card_frame{n:02d}.png"
        if not os.path.isfile(sp):
            print("skip missing", sp); continue
        p = gen(sp, f"{dst}/card_frame{n:02d}.png", pal)
        print("wrote", p, pal)


if __name__ == "__main__":
    main()
