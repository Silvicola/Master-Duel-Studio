"""生成「官方」边框叠加素材。

素材底本是 tools/overframe/border_official.orig.png（704×1024，10 张官方超框卡
截图取中位数后缩放）。中位数能抹掉各卡不同的卡面，但**边框本身仍带主体渗色**：
左右两条边从上到下能看到两次以上的色相往返，那是主体（粉紫/蓝的破碎纹理）隔着
半透明边框透上来的结果，不是官方边框真实的颜色。

实测出来的真实结构（沿周长顺时针，角度用 hue 表示）：

    左上角 12° → 顶边中心 ~62° → 右上角 177°
    右边 177° → 372°(=12°)          右下角 12°
    底边 12° → 537°(=177°)          左下角 177°
    左边 177° → 732°(=12°)          回到左上角 12°

四个角两两吻合，整圈**单调递增 720 度**，也就是沿周长刚好走两圈彩虹。
这就是官方边框的全部色彩逻辑。左右两侧中段读到的那些 305°/61° 之类，
是污染叠在这条单调曲线上的来回摆动，不是真的色相跳变。

所以生成方式：

1. 沿周长参数 t 把一圈分成四段（上/右/下/左），每段给定起止色相；
2. 段内不画直线，而是取实测剖面里**只保留正向增量**的形状（负增量视为污染，
   回退量平均后归零），再缩放到该段的目标增量。这样保留了官方那种
   「有的地方换色快、有的地方慢」的节奏，又不会出现来回翻色；
3. 饱和度按实测分区（顶边 0.24 最艳，侧边 0.19，底边 0.21），下限硬夹在
   0.10 以上，防止又退化成白缝；明度 0.90 起，由外到内轻微衰减做弧面；
4. alpha 只在最内侧 1px 淡出，让底下原生深色外沿透出来当内线。

输出 tools/overframe/border_official.png（704×1024 RGBA，不透明边框带）。
"""

import os
import numpy as np
from PIL import Image

MEDIAN = "tools/overframe/border_official.orig.png"
OUT = "tools/overframe/border_official.png"

W, H = 704, 1024
R = 27          # 卡面圆角（704 坐标）
BAND = 28       # 叠加带宽：盖满原生深色外沿（约 26px）
FEATHER = 1.0   # 内侧淡出宽度，太大就会漏出底下深色外沿，看着发黑

# 取色用的深度区间（避开最外 AA 与内侧暗边）
SAMPLE_D0, SAMPLE_D1 = 4, 15

# 整圈色相总增量。实测四段相加 ≈ 720，正好回到起点；分段增量见 HUE_STEPS。
HUE_START = 12.0
# 四段各自的增量（上/右/下/左）。按边长与实测分配，和 = 720。
HUE_STEPS = (165.0, 195.0, 165.0, 195.0)

# 饱和度：实测 上 0.24 / 右 0.19 / 下 0.21 / 左 0.19。
# 这里作为「每段的整体量级」使用，实测剖面只贡献相对节奏。
SAT_BY_SEG = (0.240, 0.190, 0.210, 0.190)
SAT_LO = 0.100      # 下限硬保护，禁止退化成白缝
SAT_HI = 0.250

# 明度：按四条边实测的亮度对齐（top 218 / right 199 / bottom 196 / left 196），
# 数值是 HSV 的 V，由 build 时的自检打印反推。
VAL_BY_SEG = (0.958, 0.932, 0.880, 0.918)
VAL_DECAY = 0.045   # 由外到内的明度衰减，模拟弧面

# 内侧收边线：官方边框在贴到卡面前有一道约 5px 的内凹阴影（实测亮度从 205
# 掉到 132，再回升 184），是靠它把边框和卡面「切」开的。少了它，边框会像
# 一层贴纸浮在卡面上。只在带的内侧 4~9px 处压暗，不碰卡面。
INNER_SHADOW = True
INNER_SHADOW_FROM = 5.0     # 距内边缘的深度（从 BAND 往回数）
INNER_SHADOW_TO = 0.0
INNER_SHADOW_DEPTH = 0.30   # 最深压到 70% 亮度
INNER_SHADOW_EDGE = 0.14    # 紧贴内侧再压一点，做硬边

# 每段剖面平滑半径（像素），只留缓慢节奏
SMOOTH_PX = 40


def _sdf():
    ys, xs = np.mgrid[0:H, 0:W]
    qx = np.abs(xs - W / 2) - (W / 2 - R)
    qy = np.abs(ys - H / 2) - (H / 2 - R)
    out = np.sqrt(np.clip(qx, 0, None) ** 2 + np.clip(qy, 0, None) ** 2)
    ins = np.minimum(np.maximum(qx, qy), 0.0)
    return out + ins - R


def _perimeter_t():
    """沿周长归一化位置 t∈[0,1)，t=0 在左上角，顺时针绕一圈。"""
    ys, xs = np.mgrid[0:H, 0:W]
    dl = xs.astype(np.float64)
    dr = (W - 1 - xs).astype(np.float64)
    dt = ys.astype(np.float64)
    db = (H - 1 - ys).astype(np.float64)
    m = np.minimum(np.minimum(dl, dr), np.minimum(dt, db))
    P = 2.0 * (W + H)
    t = np.zeros((H, W), dtype=np.float64)
    sel = m == dt
    t[sel] = (xs[sel] + R) / P
    sel = m == dr
    t[sel] = (W + R + ys[sel]) / P
    sel = m == db
    t[sel] = (W + H + R + (W - 1 - xs[sel])) / P
    sel = m == dl
    t[sel] = (2 * W + H + R + (H - 1 - ys[sel])) / P
    return np.mod(t, 1.0)


def _segment_bounds():
    P = 2.0 * (W + H)
    return [
        (0.0, (W + R) / P),
        ((W + R) / P, (W + H + R) / P),
        ((W + H + R) / P, (2 * W + H + R) / P),
        ((2 * W + H + R) / P, 1.0),
    ]


def _rgb_to_hsv(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    dl = np.maximum(mx - mn, 1e-6)
    h = np.zeros_like(mx)
    m = mx == r
    h[m] = ((g[m] - b[m]) / dl[m]) % 6.0
    m = (mx == g) & (mx != r)
    h[m] = (b[m] - r[m]) / dl[m] + 2.0
    m = (mx == b) & (mx != r) & (mx != g)
    h[m] = (r[m] - g[m]) / dl[m] + 4.0
    s = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    return h * 60.0, s, mx


def _hsv_to_rgb(h, s, v):
    hh = np.mod(h, 360.0) / 60.0
    i = np.floor(hh).astype(int) % 6
    f = hh - np.floor(hh)
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))
    out = np.zeros(hh.shape + (3,), dtype=np.float64)
    for k, (rr, gg, bb) in enumerate(
            ((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q))):
        m = i == k
        out[m, 0] = rr[m]
        out[m, 1] = gg[m]
        out[m, 2] = bb[m]
    return out


def _smooth(x, k):
    k = max(int(k) | 1, 1)
    ker = np.ones(k) / k
    return np.convolve(np.pad(x, (k // 2, k // 2), mode="edge"), ker, "valid")[:len(x)]


def _mono_shape(profile: np.ndarray) -> np.ndarray:
    """把一段色相剖面变成单调递增的形状，返回 0..1 的归一化进度。

    污染造成的回退（负增量）被平均掉，只留正向增量的节奏。
    """
    h = np.unwrap(np.radians(profile)) * 180.0 / np.pi
    h = h - h[0]
    d = np.diff(h)
    d = np.maximum(d, 0.0)
    d = _smooth(d, SMOOTH_PX)
    acc = np.concatenate([[0.0], np.cumsum(d)])
    span = acc[-1]
    if span < 1e-3:                      # 这段读不出节奏，退回线性
        return np.linspace(0.0, 1.0, len(profile))
    return acc / span


def _build_curves(med):
    """为四段各生成 (进度形状, 饱和度, 明度) 三条 1D 曲线。"""
    d = _sdf()
    depth = -d
    t = _perimeter_t()
    band = (depth >= SAMPLE_D0) & (depth <= SAMPLE_D1)

    bounds = _segment_bounds()
    out = []
    for i, (t0, t1) in enumerate(bounds):
        m = band & (t >= t0) & (t < t1)
        if i == 3:
            m = band & (t >= t0)
        n = max(2, W if i % 2 == 0 else H)

        # 把该段像素按段内位置分桶
        u = (t[m] - t0) / max(t1 - t0, 1e-9)
        idx = np.clip((u * n).astype(int), 0, n - 1)
        acc = np.zeros((n, 3))
        cnt = np.zeros(n)
        np.add.at(acc, idx, med[m])
        np.add.at(cnt, idx, 1.0)
        good = cnt > 0
        curve = np.zeros((n, 3))
        if good.any():
            curve[good] = acc[good] / cnt[good][:, None]
            pos = np.where(good)[0]
            for j in np.where(~good)[0]:
                curve[j] = curve[pos[np.argmin(np.abs(pos - j))]]
        else:
            curve[:] = med[band].mean(0)

        hue, sat, val = _rgb_to_hsv(curve)
        shape = _mono_shape(hue)
        out.append((shape, sat, val))
    return out


def main():
    src = Image.open(MEDIAN).convert("RGB")
    if src.size != (W, H):
        src = src.resize((W, H), Image.LANCZOS)
    med = np.asarray(src, dtype=np.float64) / 255.0

    curves = _build_curves(med)
    bounds = _segment_bounds()
    t = _perimeter_t()
    depth = -_sdf()

    # 先把每段的 (进度, 饱和, 明度) 拼成整圈的一维曲线
    npts = 4096
    tt = np.linspace(0.0, 1.0, npts, endpoint=False)
    hue_c = np.zeros(npts)
    sat_c = np.zeros(npts)
    val_c = np.zeros(npts)
    for i, (t0, t1) in enumerate(bounds):
        m = (tt >= t0) & ((tt < t1) if i < 3 else (tt <= 1.0))
        if not m.any():
            continue
        u = (tt[m] - t0) / max(t1 - t0, 1e-9)
        shape, sat, val = curves[i]
        xs = np.linspace(0.0, 1.0, len(shape))
        prog = np.interp(u, xs, shape)
        hue_c[m] = HUE_START + sum(HUE_STEPS[:i]) + prog * HUE_STEPS[i]

        # 饱和/明度：保留实测的**相对**起伏（归一成 0..1 的偏移），
        # 再按该段的实测整体量级还原，避免绝对量被污染带偏。
        s_sm = _smooth(sat, SMOOTH_PX)
        v_sm = _smooth(val, SMOOTH_PX)

        def _offset(x, lo, hi, amp):
            if hi - lo < 1e-6:
                return np.zeros_like(x)
            mid = 0.5 * (hi + lo)
            return amp * (x - mid) / (0.5 * (hi - lo))

        sat_c[m] = SAT_BY_SEG[i] + _offset(
            np.interp(u, xs, s_sm), float(s_sm.min()), float(s_sm.max()), 0.030)
        val_c[m] = VAL_BY_SEG[i] + _offset(
            np.interp(u, xs, v_sm), float(v_sm.min()), float(v_sm.max()), 0.028)

    # 饱和度/明度按段混合到实测水平，并做下限保护
    sat_c = np.clip(sat_c, SAT_LO, SAT_HI)

    # 整圈再做一次大半径平滑，让段与段之间过渡自然
    sat_c = _smooth(sat_c, 161)
    val_c = _smooth(val_c, 161)
    sat_c = np.clip(sat_c, SAT_LO, SAT_HI)

    # 按周长参数取色
    idx = np.clip((t * npts).astype(int), 0, npts - 1)
    hue = hue_c[idx]
    sat = sat_c[idx]
    val = np.clip(val_c[idx], 0.0, 1.0) * (1.0 - VAL_DECAY * np.clip(depth / BAND, 0, 1))

    # 内侧收边线：贴到卡面前压一道内凹阴影，把边框和卡面「切」开。
    # 位置按「离内边缘的深度」算，正好落在 BAND 之前的 4~5px 上。
    if INNER_SHADOW:
        back = BAND - depth                      # 离内边缘还有多远
        t_in = np.clip((back - INNER_SHADOW_TO) /
                       max(INNER_SHADOW_FROM - INNER_SHADOW_TO, 1e-6), 0, 1)
        # 中间最深，两端收口
        shadow = np.sin(t_in * np.pi) ** 0.7
        shadow = np.where(back <= INNER_SHADOW_FROM, shadow, 0.0)
        val = val * (1.0 - INNER_SHADOW_DEPTH * shadow)
        # 紧贴内侧再压一道窄硬边
        edge = np.clip(1.0 - back / 1.6, 0, 1)
        val = val * (1.0 - INNER_SHADOW_EDGE * edge)

    rgb = np.clip(_hsv_to_rgb(hue, sat, val), 0.0, 1.0)

    # alpha：带内不透明，仅内侧 FEATHER 像素淡出
    inside = depth >= 0
    a = np.zeros((H, W), dtype=np.float64)
    a[inside & (depth <= BAND - FEATHER)] = 1.0
    ramp = inside & (depth > BAND - FEATHER) & (depth <= BAND)
    a[ramp] = np.clip((BAND - depth[ramp]) / FEATHER, 0, 1)

    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[..., 0:3] = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
    arr[..., 3] = np.clip(a * 255.0, 0, 255).astype(np.uint8)

    img = Image.fromarray(arr, "RGBA")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT)

    # 便于肉眼核对：四条带的平均色与饱和度
    pix = np.asarray(img).astype(np.float64)
    al = pix[..., 3] > 200
    ys, xs = np.mgrid[0:H, 0:W]
    for name, m in (("top", (ys < BAND) & al), ("bottom", (ys >= H - BAND) & al),
                    ("left", (xs < BAND) & al), ("right", (xs >= W - BAND) & al)):
        c = pix[..., :3][m]
        mx = c.max(1)
        mn = c.min(1)
        sat_mean = ((mx - mn) / np.maximum(mx, 1)).mean()
        print(f"  {name:7s} rgb={[round(x) for x in c.mean(0)]} sat={sat_mean:.3f}")
    print("wrote", OUT)


if __name__ == "__main__":
    main()