"""取帧 / 抠图 / 超分 / 拼图集"""
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from mdstudio import runtime
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr
from mdstudio.progress import step, tick

log = logging.getLogger("mdstudio")

MAX_TEX = 16384          # 单边上限, 超了 GPU 不认
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError as e:
        raise MDStudioError(tr("video features need extra packages. Run:  "
                              "python -m pip install opencv-python-headless")) from e


def _resize(arr: np.ndarray, size: tuple) -> np.ndarray:
    """缩放一帧. 4 通道时先乘 alpha 再缩, 否则透明区的颜色会渗到边缘 (抠出来一圈黑边)"""
    if arr.ndim == 3 and arr.shape[2] == 4:
        a = arr[..., 3:4].astype(np.float32) / 255.0
        pre = np.dstack([arr[..., :3].astype(np.float32) * a, arr[..., 3:4].astype(np.float32)])
        small = np.array(Image.fromarray(pre.astype(np.uint8), "RGBA").resize(size, Image.LANCZOS))
        a2 = small[..., 3:4].astype(np.float32) / 255.0
        rgb = small[..., :3].astype(np.float32)
        rgb = np.where(a2 > 0.004, rgb / np.maximum(a2, 1e-3), rgb)
        return np.dstack([np.clip(rgb, 0, 255), small[..., 3:4]]).astype(np.uint8)
    return np.array(Image.fromarray(arr).resize(size, Image.LANCZOS))


def sample(source, n: int) -> list:
    """视频 / gif / 帧文件夹 -> 均匀取 n 帧, RGBA uint8 (带透明通道的素材原样保留)"""
    src = Path(source)
    if src.is_dir():
        files = sorted(p for p in src.iterdir() if p.suffix.lower() in IMG_EXT)
        if not files:
            raise MDStudioError(tr("no images in %s") % src)
        idx = np.linspace(0, len(files) - 1, min(n, len(files))).round().astype(int)
        step(tr("reading %s image(s) from %s"), len(idx), src.name)
        out = []
        for k, i in enumerate(idx):
            out.append(np.array(Image.open(files[i]).convert("RGBA")))
            tick(tr("reading frames"), k + 1, len(idx))
        return out
    cv2 = _cv2()
    cap = cv2.VideoCapture(str(src))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        raise MDStudioError(tr("can't read %s") % src)
    step(tr("reading %s frame(s) from %s"), n, src.name)
    out = []
    for k, fi in enumerate(np.linspace(0, total - 1, n).round().astype(int)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fi))
        ok, f = cap.read()
        if ok:
            out.append(np.dstack([cv2.cvtColor(f, cv2.COLOR_BGR2RGB),
                                  np.full(f.shape[:2], 255, np.uint8)]))
        tick(tr("reading frames"), k + 1, n)
    cap.release()
    return out


_SESSION = None


def matting_model_path():
    """isnet-anime.onnx 在哪: 环境变量 > 包内 tools/matting > 用户缓存目录.

    包内优先: 便携版自带 176MB 的那份, 就该用它, 免得用户缓存里的旧模型
    (或者下到一半的残文件) 把随包的盖掉 —— 便携原则是不读程序目录之外的东西。
    """
    p = os.environ.get("MDSTUDIO_MATTING_MODEL")
    if p and Path(p).is_file():
        return Path(p)
    home = Path(os.environ.get("U2NET_HOME") or (Path.home() / ".u2net"))
    for cand in (runtime.bundle_root() / "tools" / "matting" / "isnet-anime.onnx",
                 home / "isnet-anime.onnx"):
        if cand.is_file():
            return cand
    return None


def _matte_session():
    """onnxruntime + isnet-anime.

    不用 rembg: 它为了便利封装把 scipy / numba / llvmlite / scikit-image 一起拖进来
    (实测 site-packages 多占 472MB), 而这些对推理毫无用处. 模型本身就是 ONNX.
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise MDStudioError(tr("cutting the character out needs onnxruntime. Run:  "
                              "python -m pip install onnxruntime   (or pick mode square / full)")) from e
    model = matting_model_path()
    if model is None:
        raise MDStudioError(tr("background-removal model not found. Put isnet-anime.onnx in %s")
                           % (Path.home() / ".u2net"))
    step(tr("loading the background-removal model"))
    so = ort.SessionOptions()
    so.log_severity_level = 3
    _SESSION = ort.InferenceSession(str(model), sess_options=so,
                                    providers=["CPUExecutionProvider"])
    return _SESSION


def _subject_alpha(sess, frame) -> np.ndarray:
    """isnet-anime 出 alpha. 它固定吃 1024x1024, 单通道输出.

    归一化按 rembg 对 isnet 的写法: 先除以本图最大值, 再 (x/mean - 1)/std.
    换成标准 ImageNet 那套 (x-mean)/std 会输出全零, 这个模型只认前者.
    """
    h, w = frame.shape[:2]
    im = Image.fromarray(frame[..., :3]).resize((1024, 1024), Image.BILINEAR)
    a = np.asarray(im, dtype=np.float32)
    mx = float(a.max()) or 255.0
    a = a / mx
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    a = (a / mean - 1.0) / std
    a = np.transpose(a, (2, 0, 1))[None].astype(np.float32)

    name = sess.get_inputs()[0].name
    out = np.asarray(sess.run(None, {name: a})[0]).reshape(1024, 1024)

    lo, hi = float(out.min()), float(out.max())
    out = (out - lo) / (hi - lo) if hi > lo else np.zeros_like(out)
    mask = Image.fromarray((out * 255).astype(np.uint8), mode="L")
    return np.asarray(mask.resize((w, h), Image.BILINEAR), dtype=np.uint8)


def bg_color_of(raw) -> np.ndarray:
    """从原帧四边采样幕布色. 边框一般就是纯背景, 取中位数不怕个别噪点"""
    b = 16
    px = raw[..., :3] if raw.ndim == 3 else raw
    edge = np.concatenate([px[:b, :].reshape(-1, 3), px[-b:, :].reshape(-1, 3),
                           px[:, :b].reshape(-1, 3), px[:, -b:].reshape(-1, 3)])
    return np.median(edge, axis=0)


def is_key_bg(c) -> bool:
    """边框颜色够纯、够饱和, 就当它是幕布 (绿/蓝/品红/红都行, 不写死颜色)"""
    r, g, b = float(c[0]), float(c[1]), float(c[2])
    mx, mn = max(r, g, b), min(r, g, b)
    return mx >= 120 and (mx - mn) >= 90


def key_channels(bg) -> tuple:
    """幕布色的主通道. 绿=(1,) 蓝=(2,) 品红=(0,2) 红=(0,)"""
    r, g, b = (float(bg[0]), float(bg[1]), float(bg[2]))
    mx = max(r, g, b)
    return tuple(i for i, v in enumerate((r, g, b)) if v >= mx * 0.75)


def despill(rgba: np.ndarray, bg=None) -> np.ndarray:
    """按幕布色软键控 + 清掉透出来的幕布色.

    bg 为 None 时什么都不做 (没有幕布, 交给 AI 抠像). 有幕布时:
      1. 越接近幕布色 alpha 越低 (35 以内判背景, 110 以外原样) —— 边缘自动干净;
      2. 幕布主通道的强色像素 alpha 归零, 其余把该通道压到其他通道的最大值以内.
    主通道由 key_channels() 从幕布色算出来, 所以绿幕/蓝幕/品红幕/红幕通用.
    """
    if bg is None or not is_key_bg(bg):
        return rgba
    kc = key_channels(bg)
    others = tuple(c for c in range(3) if c not in kc)
    if not others:                      # 白/灰幕布没有"主通道", 只做软键控
        return rgba
    c = rgba[..., :3].astype(np.float32)
    d = np.linalg.norm(c - np.asarray(bg, np.float32), axis=-1)
    soft = np.clip((d - 35.0) / (110.0 - 35.0), 0.0, 1.0)
    rgba[..., 3] = np.clip(rgba[..., 3].astype(np.float32) * soft, 0, 255).astype(np.uint8)
    chans = [rgba[..., i].astype(np.int16) for i in range(3)]
    peak = np.maximum.reduce([chans[i] for i in others])
    hit = np.ones(peak.shape, bool)
    for i in kc:
        hit &= chans[i] > peak + 45
        hit &= chans[i] > 90
    rgba[..., 3][hit] = 0
    for i in kc:
        chans[i][:] = np.minimum(chans[i], peak)
        rgba[..., i] = chans[i]
    return rgba


KEY_GREEN = np.array([0.0, 254.0, 0.0], np.float32)
KEY_BLUE = np.array([0.0, 0.0, 255.0], np.float32)


def _ring(px, m: int):
    """取「距边 m 像素」那一圈像素; 太小就返回 None"""
    h, w = px.shape[:2]
    sub = px[m:h - m, m:w - m]
    if sub.shape[0] < 48 or sub.shape[1] < 48:
        return None
    b = 16
    return np.concatenate([sub[:b, :].reshape(-1, 3), sub[-b:, :].reshape(-1, 3),
                           sub[:, :b].reshape(-1, 3), sub[:, -b:].reshape(-1, 3)]).astype(np.float32)


def pick_key_color(raw, key="auto"):
    """判断素材有没有幕布, 有就返回幕布色, 没有返回 None (交给 AI 抠像).

    auto: 从外往里试三圈边框 (最外圈、往里 6%、12%), 哪一圈颜色够纯够均匀就当幕布.
          试多圈是为了兼容黑边/信箱边这种「真正的内容不在最外层」的情况.
    普通画面不会被误判: 除了颜色要饱和, 还要求那一圈有 60% 以上像素都接近这个色.
    """
    k = str(key).lower()
    if k == "green":
        return KEY_GREEN
    if k == "blue":
        return KEY_BLUE
    px = raw[..., :3] if raw.ndim == 3 else raw
    h, w = px.shape[:2]
    for m in (0, int(min(h, w) * 0.06), int(min(h, w) * 0.12)):
        edge = _ring(px, m)
        if edge is None:
            continue
        med = np.median(edge, axis=0)
        if not is_key_bg(med):
            continue
        close = (np.linalg.norm(edge - med, axis=-1) < 80).mean()
        if close >= 0.6:
            return med
    return None


def _crop_union(frames, crop_pad: float = 1.04) -> list:
    """按所有帧 alpha>30 的并集包围盒裁成正方形.

    用并集而不是逐帧裁: 角色在每帧位置不同, 逐帧裁会让画面抖.
    """
    box = None
    for r in frames:
        ys, xs = np.where(r[..., 3] > 30)
        if len(xs):
            b = [xs.min(), ys.min(), xs.max(), ys.max()]
            box = b if box is None else [min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3])]
    if box is None:
        raise MDStudioError(tr("background removal found no character in any frame"))
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    side = int(max(box[2] - box[0], box[3] - box[1]) * crop_pad)
    sx, sy = int(cx - side / 2), int(cy - side / 2)
    h, w = frames[0].shape[:2]
    out = []
    for r in frames:
        c = np.zeros((side, side, 4), np.uint8)
        ax0, ay0, ax1, ay1 = max(sx, 0), max(sy, 0), min(sx + side, w), min(sy + side, h)
        c[ay0 - sy:ay1 - sy, ax0 - sx:ax1 - sx] = r[ay0:ay1, ax0:ax1]
        out.append(c)
    return out


def crop_alpha(frames, crop_pad: float = 1.04) -> list:
    """素材自带透明通道时用: 不抠像, 只按包围盒收紧画面"""
    return _crop_union(frames, crop_pad)


def matte(frames, crop_pad=1.04, key="auto") -> list:
    """抠图 + 按所有帧的并集包围盒裁成正方形 (角色在每帧位置一致, 不会抖)

    key: auto / green / blue —— 哪种幕布. auto 会从边框自己判断.
    """
    sess = _matte_session()
    raw = []
    step(tr("cutting out the character in %s frame(s) (slowest part)"), len(frames))
    for i, f in enumerate(frames):
        a = np.dstack([f[..., :3], _subject_alpha(sess, f)])
        raw.append(despill(a, pick_key_color(f, key)))
        frames[i] = None   # 省内存, 16G 机器 N 一大就 OOM
        tick(tr("cutting out the character"), i + 1, len(frames))
    return _crop_union(raw, crop_pad)


def opaque(frames, cell: int, full: bool) -> list:
    """不抠图. full=True 保留原比例 (cell = 高), False 中心裁正方形.

    帧本身带透明通道时原样保留, 不会强行涂成不透明.
    """
    cv2 = _cv2()
    out = []
    step(tr("resizing %s frame(s)"), len(frames))
    for k, f in enumerate(frames):
        tick(tr("resizing"), k + 1, len(frames))
        h, w = f.shape[:2]
        if full:
            ch, cw = cell, max(4, int(round(cell * w / h / 4)) * 4)   # BC7 要 4 的倍数
        else:
            s = min(h, w)
            f = f[(h - s) // 2:(h - s) // 2 + s, (w - s) // 2:(w - s) // 2 + s]
            ch = cw = cell
        f = _resize(f, (cw, ch))
        if f.ndim == 2 or f.shape[2] == 3:
            f = np.dstack([f, np.full(f.shape[:2], 255, np.uint8)])
        out.append(f)
    return out


def has_alpha(frames) -> bool:
    """素材自己带透明通道吗 (帧文件夹常见: 已经是抠好的 PNG 序列)"""
    for f in frames:
        if f.ndim == 3 and f.shape[2] == 4 and (f[..., 3] < 250).any():
            return True
    return False


def esrgan_exe():
    """Real-ESRGAN ncnn-vulkan 独立版的 exe 路径.
    优先 MDSTUDIO_ESRGAN 环境变量(可指向任意路径), 否则用包内 tools/realesrgan."""
    p = os.environ.get("MDSTUDIO_ESRGAN")
    if p and Path(p).is_file():
        return Path(p)
    exe = runtime.bundle_root() / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan.exe"
    return exe if exe.is_file() else None


def sharpen_available() -> bool:
    return esrgan_exe() is not None


def sharpen(frames, cell: int, model: str = "realesr-animevideov3", scale: int = 4) -> list:
    """超分: 先放大 scale 倍, 再缩回 cell.

    scale 只影响中间重建精度, 成品像素大小始终由 cell 决定.
    用 ncnn-vulkan 独立版(不依赖 torch/basicsr, 走显卡 Vulkan):
    整批帧写进临时目录一次跑完, 比逐帧起进程快很多.
    只处理 RGB, alpha 单独从原帧缩放过来(几何一致, 不会错位).
    """
    exe = esrgan_exe()
    if exe is None:
        log.warning(tr("Real-ESRGAN not found, skipping sharpen"))
        return frames
    tmp_in = Path(tempfile.mkdtemp(prefix="mdck_esr_in_"))
    tmp_out = Path(tempfile.mkdtemp(prefix="mdck_esr_out_"))
    try:
        names = []
        for i, f in enumerate(frames):
            n = f"{i:04d}.png"
            Image.fromarray(f[..., :3]).save(tmp_in / n)
            names.append(n)
        step(tr("upscaling %s frame(s) %sx (GPU)"), len(frames), scale)
        cmd = [str(exe), "-i", str(tmp_in), "-o", str(tmp_out), "-n", model,
               "-s", str(int(scale)), "-f", "png"]
        # 它是控制台程序, 不加 CREATE_NO_WINDOW 会在桌面弹一个黑窗
        flags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(exe).parent),
                           creationflags=flags)
        if r.returncode != 0:
            # 显卡/Vulkan 不可用时不要静默降级成糊图, 直接把原因抛出来
            raise MDStudioError(tr("Real-ESRGAN failed: %s") % (r.stderr.strip()[-300:] or r.returncode))
        out = []
        for i, f in enumerate(frames):
            up = Image.open(tmp_out / names[i]).convert("RGB")
            h, w = f.shape[:2]
            size = (cell, max(4, int(round(cell * h / w)))) if w >= h else (max(4, int(round(cell * w / h))), cell)
            rgb = np.array(up.resize(size, Image.LANCZOS))
            a = np.array(Image.fromarray(f[..., 3]).resize(size, Image.LANCZOS))
            out.append(np.dstack([rgb, a]))
            tick(tr("upscaling"), i + 1, len(frames))
            frames[i] = None
        return out
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)


def fit(frames, cell: int) -> list:
    """统一缩到 cell (长边), 保持比例"""
    out = []
    for f in frames:
        h, w = f.shape[:2]
        size = (cell, max(4, int(round(cell * h / w)))) if w >= h else (max(4, int(round(cell * w / h))), cell)
        if (w, h) != size:
            f = _resize(f, size)
        out.append(f)
    return out


def fit_height(frames, cell: int) -> list:
    """高 = cell, 宽按比例 (4 的倍数, BC7). full 模式用: cell 指的是帧高"""
    out = []
    for f in frames:
        h, w = f.shape[:2]
        size = (max(4, int(round(cell * w / h / 4)) * 4), cell)
        if (w, h) != size:
            f = _resize(f, size)
        out.append(f)
    return out


def grid(n: int, fw: int, fh: int, cols=None):
    cols = cols or max(1, round(n ** 0.5))
    while cols > 1 and cols * fw > MAX_TEX:
        cols -= 1
    rows = (n + cols - 1) // cols
    if cols * fw > MAX_TEX or rows * fh > MAX_TEX:
        raise MDStudioError(tr("sheet %sx%s is over the %spx limit. Use fewer frames or a smaller size.")
                           % (cols * fw, rows * fh, MAX_TEX))
    return cols, rows


def edge_bleed(rgba: np.ndarray, iters=6) -> np.ndarray:
    """直通 alpha + 双线性采样时, 透明区的 RGB 会被采到 -> 边缘一圈亮边. 把边缘颜色往外扩几圈"""
    cv2 = _cv2()
    rgb = np.ascontiguousarray(rgba[..., :3])
    solid = (rgba[..., 3] > 0).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    for _ in range(iters):
        if solid.all():
            break
        d_rgb, d_solid = cv2.dilate(rgb, k), cv2.dilate(solid, k)
        new = (d_solid > 0) & (solid == 0)
        rgb[new] = d_rgb[new]
        solid[new] = 1
    out = rgba.copy()
    out[..., :3] = rgb
    return out


def pack(frames, premultiply: bool, cols=None, alphas=None):
    """-> (sheet, cols, rows, regions[(name,x,y,w,h)])

    premultiply=True : cut-in (Spine atlas pma:true)
    premultiply=False: run-effect 粒子 (URP alpha blend, 直通 alpha + edge_bleed)
    alphas: 每帧透明度 (0..1). 淡入淡出直接烘进帧图, 不用 Spine 的槽位颜色轨道.
            那个轨道在这个游戏的运行时里会让整份骨架加载失败 (2026-10-22 实测:
            加上 color 轨道后整张图集被当成一张图铺开)
    """
    n = len(frames)
    fh, fw = frames[0].shape[:2]
    cols, rows = grid(n, fw, fh, cols)
    sheet = Image.new("RGBA", (cols * fw, rows * fh), (0, 0, 0, 0))
    regions = []
    step(tr("building the %sx%s sheet (%s frames)"), cols * fw, rows * fh, n)
    for i in range(n):
        tick(tr("building the sheet"), i + 1, n)
        a = frames[i]
        if premultiply:
            a = a.astype(np.float32)
            a[..., :3] *= a[..., 3:4] / 255.0
            if alphas is not None:
                k = float(alphas[i])
                if k < 1.0:
                    a *= k      # pma 下 rgb 和 alpha 必须同步缩, 否则边缘发亮
            a = a.clip(0, 255).astype(np.uint8)
        else:
            a = edge_bleed(a)
        x, y = (i % cols) * fw, (i // cols) * fh
        sheet.paste(Image.fromarray(a, "RGBA"), (x, y))
        regions.append((f"frame_{i}", x, y, fw, fh))
        frames[i] = None
    return sheet, cols, rows, regions


def preview_dark(sheet: Image.Image, premultiplied: bool) -> Image.Image:
    # 白底预览会把白边藏起来, 一定看深色底
    s = np.array(sheet).astype(np.float32)
    a = s[..., 3:4] / 255.0
    fg = s[..., :3] if premultiplied else s[..., :3] * a
    return Image.fromarray((np.full(s.shape[:2] + (3,), 40, np.float32) * (1 - a) + fg).clip(0, 255).astype(np.uint8))
