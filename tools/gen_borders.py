"""生成三种超框外沿特效的叠加素材（不随卡型变化，整张卡通用）。

- border_rainbow.png : 虹彩 —— 彩色环带，颜色取自作者工具导出的成品 17767.png。
- border_glory.png   : 幻彩 —— 同族柔和淡彩环带，颜色取自 16493.png。
- border_soft.png    : 官方 —— 软发光珍珠膜，沿圆角外沿，参考 of1/of2 的游戏官方超框卡。

三者都只覆盖最外一圈（原生卡框深色外沿约 26px），其余透明，合成时盖在原生卡框之上。
"""

import os
from pathlib import Path

import numpy as np
from PIL import Image

# 参考图来自本地旧素材目录（第三方成品图，不入库，也不该写死作者机器上的路径）。
# 要重跑本脚本时用环境变量 MDSTUDIO_OF3 指向那个目录，默认找当前目录下的 OF3/。
_OF3 = Path(os.environ.get("MDSTUDIO_OF3", "OF3")) / "DIST_Despia_Complete"


W, H = 704, 1024
# 游戏里卡框的实际圆角半径。用游戏截图做圆拟合：卡面宽约 894px（1.27 倍纹理），
# 转角圆弧半径约 33 截图像素，换算到 704 纹理空间约 26px。
# 之前用 36 是按实体卡比例估的，比游戏渲染圆；改用实测的 26 才贴得上卡框。
R = 26


def _grid_sdf():
    """圆角矩形有向距离场：<0 在卡内，>0 在卡外。向量化。"""
    ys, xs = np.mgrid[0:H, 0:W]
    qx = np.abs(xs - W / 2) - (W / 2 - R)
    qy = np.abs(ys - H / 2) - (H / 2 - R)
    out = np.sqrt(np.clip(qx, 0, None) ** 2 + np.clip(qy, 0, None) ** 2)
    ins = np.minimum(np.maximum(qx, qy), 0.0)
    return out + ins - R


def _perimeter_colors(ref_path, sample):
    """从作者工具导出的成品纹理里，沿卡边取一圈颜色。

    把每个像素投到卡边上：先把坐标夹进「半径内缩后的矩形」得到基点，再沿
    从基点指向像素的方向走 R，落点就是卡边上的最近点。圆角处连续无接缝。
    再从落点沿同方向往内退 sample 取样，得到只随周长变化的颜色场。

    这套成品纹理的配色规律是：左右两边沿卡高走两轮色相、互相镜像，
    上下两边固定红粉。直接照搬就得到作者的原色，不用自己调。
    """
    ref = np.asarray(Image.open(ref_path).convert("RGB")).astype(np.float32)
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    qx = np.clip(xs, R, W - 1 - R)
    qy = np.clip(ys, R, H - 1 - R)
    vx = xs - qx
    vy = ys - qy
    n = np.hypot(vx, vy)
    n[n == 0] = 1.0
    ex = np.clip(qx + R * vx / n, 0, W - 1)
    ey = np.clip(qy + R * vy / n, 0, H - 1)
    sxi = np.clip(np.round(ex - sample * vx / n).astype(int), 0, W - 1)
    syi = np.clip(np.round(ey - sample * vy / n).astype(int), 0, H - 1)
    return ref[syi, sxi]


def _horizontal_ramp(ref_path, d_from=3, d_to=16):
    """从成品纹理的上、下边取彩带色，重建「x -> 带色」的一维剖面。

    16493 的彩带横向也带渐变：左端偏橙、右端偏蓝紫。
    它的左右两边被主体压住，单点取样会变成一条条横向彩条，所以横向剖面只用上下两边。
    """
    ref = np.asarray(Image.open(ref_path).convert("RGB")).astype(np.float32)
    rows = []
    for d in range(d_from, d_to + 1):
        rows.append(ref[d, :, :])
        rows.append(ref[H - 1 - d, :, :])
    prof = np.median(np.stack(rows), axis=0)          # (W, 3)

    # 沿 x 环形平滑，压掉残留噪点（色相本身是缓变的，平滑不会伤到走向）
    k = 9
    pad = np.concatenate([prof[-k:], prof, prof[:k]], axis=0)
    ker = np.ones(k) / k
    for c in range(3):
        prof[:, c] = np.convolve(pad[:, c], ker, mode="same")[k:-k]
    return np.broadcast_to(prof[None, :, :], (H, W, 3)).copy()


def _vertical_ramp(ref_path, hue_src, d_from=6, d_to=15, half=80):
    """重建「y -> 带色」的纵向剖面。

    左右两条边同样是从上到下走两轮色相（红→橙→绿→青→蓝→紫→红→…），
    和虹彩的左右边是同一套走向。

    色相不是从 ref_path 取的：16493 左右两条边的下半段被主体压住，量出来是一片发闷的
    橙褐（连饱和度都掉了），继续取样只会把主体色当带色。所以色相改用同一工具、未被遮住的
    17767（hue_src），彩度与明度仍按 ref_path 上下边的均值，保持 16493 的淡彩调。
    色相在 0/255 处会回绕，按单位向量平滑。
    """
    hsv = np.asarray(Image.open(hue_src).convert("HSV")).astype(np.float32)
    h = 0.5 * (hsv[:, d_from:d_to + 1, 0].mean(axis=1)
               + hsv[:, W - 1 - d_to:W - d_from, 0].mean(axis=1))
    ang = h / 255.0 * 2 * np.pi
    cs, sn = np.cos(ang), np.sin(ang)
    ker = np.ones(2 * half + 1) / (2 * half + 1)

    def _circ(v):
        p = np.concatenate([v[-half:], v, v[:half]])
        return np.convolve(p, ker, mode="same")[half:-half]

    ang = np.arctan2(_circ(sn), _circ(cs))
    hue = ((ang / (2 * np.pi)) % 1.0) * 255.0

    # 彩度与亮度取 ref_path 上下边彩带的均值
    ref = np.asarray(Image.open(ref_path).convert("HSV")).astype(np.float32)
    sat = float(np.median(np.concatenate([ref[3:17, :, 1], ref[H - 17:H - 3, :, 1]], axis=0)))
    val = float(np.median(np.concatenate([ref[3:17, :, 2], ref[H - 17:H - 3, :, 2]], axis=0)))

    field = np.zeros((H, W, 3), dtype=np.uint8)
    field[..., 0] = np.broadcast_to(hue[:, None], (H, W)).astype(np.uint8)
    field[..., 1] = int(sat)
    field[..., 2] = int(val)
    return np.asarray(Image.fromarray(field, "HSV").convert("RGB")).astype(np.float32)


def _band_colors(ref_path, vert_hue_src):
    """按周长合成彩带颜色：靠上下边用横向剖面，靠左右边用纵向剖面，转角处混合。"""
    h_side = _horizontal_ramp(ref_path)
    v_side = _vertical_ramp(ref_path, vert_hue_src)
    ys, xs = np.mgrid[0:H, 0:W]
    dx = np.minimum(xs, W - 1 - xs).astype(np.float32)
    dy = np.minimum(ys, H - 1 - ys).astype(np.float32)
    # 1 = 竖边主导，0 = 横边主导，60px 内过渡
    w = np.clip((dy - dx) / 60.0 * 0.5 + 0.5, 0, 1)[..., None]
    return h_side * (1 - w) + v_side * w


def _inner_shade(rgb, band):
    """给彩带补上「内缘渐暗」，让内侧和外框的过渡像棱彩那样自然。

    棱彩之所以过渡自然，是因为它保留了原生卡框自己的明暗：外缘全亮，靠内压到约 70%。
    这里从棱彩素材上量出同一条曲线补过来：0..12px 基本不压，之后压到 0.68（指数 1.2）。
    收尾不再另做亮边 —— 原生卡框自己在 26..28px 就有一道斜面高光，
    再叠一道白线会变成显眼的白色环。
    """
    depth = np.clip(-_grid_sdf(), 0, band)
    t = np.clip((depth - band * 0.46) / (band * 0.54), 0, 1)
    return rgb * (1.0 - 0.32 * t ** 1.2)[..., None]


def _round_edge(rgb, opaque_to, feather_to):
    """裁出「0..opaque_to 不透明、之后 feather_to 内羽化到透明」的一圈，返回 RGBA。"""
    from PIL import ImageFilter

    depth = -_grid_sdf()
    t = np.clip((opaque_to + feather_to - depth) / max(feather_to, 1e-6), 0, 1)
    # 卡外（depth<0）一律透明；卡内（含正好压在边上的那一像素）按上面的比例，
    # 否则最外一像素会半透明，露出底下原生框的深色，看着像一道黑边。
    a = np.where(depth >= 0, 255.0 * t, 0.0)
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[..., 0:3] = np.clip(rgb, 0, 255).astype(np.uint8)
    arr[..., 3] = np.clip(a, 0, 255).astype(np.uint8)
    return np.array(Image.fromarray(arr, "RGBA").filter(ImageFilter.GaussianBlur(0.4)))


# 虹彩：颜色取自 17767.png（作者工具导出的成品）。它的彩带是完整的 26px，
# 内侧没有额外的线，只是 2px 柔化后接进画面。
IRIDESCENT_SRC = str(_OF3 / "17767.png")
IRIDESCENT_SAMPLE = 8.0
IRIDESCENT_BAND = 26.0


def make_iridescent():
    """虹彩：整条 26px 彩色环带，内侧 2px 柔化，靠内渐暗。"""
    rgb = _perimeter_colors(IRIDESCENT_SRC, IRIDESCENT_SAMPLE)
    return _round_edge(_inner_shade(rgb, IRIDESCENT_BAND), IRIDESCENT_BAND, 2.0)


def _hue_field():
    """绕卡一周的色相 0..1，顶部为 0，顺时针。"""
    ys, xs = np.mgrid[0:H, 0:W]
    ang = np.degrees(np.arctan2(ys - H / 2, xs - W / 2))
    return ((ang + 90) % 360) / 360.0


def _hsl_to_rgb_vec(h, s, l):
    """向量化 HSL(h,s,l) -> RGB uint8 (H,W,3)。h/s/l 为同形 float 数组。"""
    c = (1 - np.abs(2 * l - 1)) * s
    hp = (h * 6) % 6
    x = c * (1 - np.abs(hp % 2 - 1))
    # 三段：r,g,b 基础值
    r = np.zeros_like(h); g = np.zeros_like(h); b = np.zeros_like(h)
    m = l - c / 2
    # 用分段选择
    seg = hp.astype(int)
    # 0: r=c g=x b=0 ; 1: r=x g=c b=0 ; 2: r=0 g=c b=x
    # 3: r=0 g=x b=c ; 4: r=x g=0 b=c ; 5: r=c g=0 b=x
    conds = [
        (seg == 0, c, x, 0), (seg == 1, x, c, 0), (seg == 2, 0, c, x),
        (seg == 3, 0, x, c), (seg == 4, x, 0, c), (seg == 5, c, 0, x),
    ]
    rr = np.zeros_like(h); gg = np.zeros_like(h); bb = np.zeros_like(h)
    for mask, rv, gv, bv in conds:
        rr = np.where(mask, rv, rr); gg = np.where(mask, gv, gg); bb = np.where(mask, bv, bb)
    rr = (rr + m) * 255; gg = (gg + m) * 255; bb = (bb + m) * 255
    return np.stack([rr, gg, bb], axis=-1).astype(np.uint8)


# 幻彩：颜色取自 16493.png（作者工具导出的成品）。它的彩带 0..21px，
# 再往里是原生外沿露出的暗线和一道浅米线；这两层用户都不要，只留彩带。
GLORY_COLOR_SRC = str(_OF3 / "16493.png")
# 左右边的纵向色相改用 17767：16493 左右边下半段被主体压住，色相不可信。
# 两者出自同一工具，纵向走法一致（用户也确认「像虹彩的左右边框」）。
GLORY_VERT_SRC = str(_OF3 / "17767.png")
GLORY_BAND = 26.0     # 彩带铺满原生外沿宽度


def make_glory():
    """幻彩：整条 26px 彩带，内侧 2px 柔化。

    上下边用 16493 的横向色相渐变（左橙 → 右蓝紫）；
    左右边用同一套工具的纵向色相渐变（从上到下走两轮），色相源为未遮挡的 17767。
    彩度与明度按 16493 上下边取，保持它的淡彩调。内侧的暗线与浅米线都不要，
    只补一条内缘渐暗，让内侧过渡像棱彩那样自然。
    """
    rgb = _band_colors(GLORY_COLOR_SRC, GLORY_VERT_SRC)
    return _round_edge(_inner_shade(rgb, GLORY_BAND), GLORY_BAND, 2.0)


def make_soft():
    """官方：软发光珍珠膜，绕卡一周换色相，只在最外 26px 内渐隐。"""
    from PIL import ImageFilter

    d = _grid_sdf()
    hue = _hue_field()
    B_inner = 26
    inside = -d
    t = np.clip(inside / B_inner, 0, 1)  # 0=边缘最亮 1=最内
    soft_in = 2.0 / B_inner  # 最外这段做渐入
    alpha = np.where(t <= 0, 0.0,
              np.where(t < soft_in, (t / soft_in) * 235,
                      235 * (1 - (t - soft_in) / (1 - soft_in)) ** 1.4))
    alpha = np.clip(alpha, 0, 235).astype(np.uint8)
    rgb = _hsl_to_rgb_vec(hue, 0.42 * np.ones_like(hue), 0.74 * np.ones_like(hue))
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    mask = alpha > 0
    arr[..., 0:3][mask] = rgb[mask]
    arr[..., 3][mask] = alpha[mask]
    return np.array(Image.fromarray(arr, "RGBA").filter(ImageFilter.GaussianBlur(1.6)))


def main():
    out_dir = "tools/overframe"
    import os
    os.makedirs(out_dir, exist_ok=True)
    for fn, arr in (("border_rainbow.png", make_iridescent()),
                    ("border_glory.png", make_glory()),
                    ("border_soft.png", make_soft())):
        Image.fromarray(arr, "RGBA").save(f"{out_dir}/{fn}")
        print("wrote", f"{out_dir}/{fn}", arr.shape)


if __name__ == "__main__":
    main()
