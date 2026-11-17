# -*- coding: utf-8 -*-
"""随包大文件的清单与按需获取。

有些资源太大，不适合塞进发布 zip（抠像模型 168MB、超分组件 43MB），打包时用
`build_portable.py --slim` 剔除，用户在设置页按需下载。

它们只影响个别功能（AI 抠图 / 超分 / Spine 版本转换），所以**不做启动门禁**：
软件照常进，动作开始前由引擎的 `assets.needs` 判一次，缺哪个就引导去设置页装。
CLI 则是直接报错。

三个必须做到的点，参考了 GameSpineTool 的处理：
  - 校验：下载完按 sha256 核对，对不上就当失败，不落盘
  - 续传：半成品文件跨尝试保留，对 github 的链路实测速度会在几十 KB/s 到几 MB/s
    之间跳，失败一次就从零重来会永远下不完
  - 可取消：分块下载，每块检查一次取消标志，已经下的部分留着

下载源写成列表，先国内镜像、失败再回退上游。地址对不上时改 ASSETS 就行，
调用方不用动。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import threading
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import runtime
from .i18n import tr


@dataclass(frozen=True)
class Asset:
    key: str
    label: str
    dest: str                       # 相对 bundle_root 的目标路径（文件或目录）
    sha256: str                     # 下载物的指纹（单文件指文件本身，zip 指 zip）
    urls: tuple
    size: int = 0                   # 预期字节，只用于界面显示；0 表示未知
    required: bool = True           # True: 有功能离不开它；False: 纯可选
    members: tuple = ()             # zip 只取这些成员 (包内路径, 目标相对路径)
    probe: str = ""                 # 判断"已就位"的标志文件（目录型资源用）
    base: str = ""                  # 逐文件下载的目录前缀（raw 链接）
    mirrors: tuple = ()             # base 的备用前缀（代理转发同一个 raw 地址）
    files: tuple = ()               # ((相对路径, 字节数, sha256), ...)

    @property
    def bases(self) -> tuple:
        """逐文件下载用到的所有目录前缀，主源在前。"""
        return ((self.base,) + tuple(self.mirrors)) if self.base else ()

    @property
    def target(self) -> Path:
        return runtime.bundle_root() / self.dest

    @property
    def marker(self) -> Path:
        return self.target if not self.probe else runtime.bundle_root() / self.probe


def _index(name: str) -> tuple:
    """从 assets_index.json 读一组文件清单（由 tools/gen_assets_index.py 生成）"""
    p = Path(__file__).with_name("assets_index.json")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    return tuple((r, int(z), h) for r, z, h in raw.get(name, ()))


# 随包素材也放仓库里，可以直接从 raw 拉。换成别的托管只要改这个前缀。
RAW = "https://raw.githubusercontent.com/Silvicola/Master-Duel-Studio/main"


ASSETS = (
    Asset(
        key="matting",
        label="AI 抠图模型",
        dest="tools/matting/isnet-anime.onnx",
        probe="tools/matting/isnet-anime.onnx",
        size=176_069_933,
        sha256="f15622d853e8260172812b657053460e20806f04b9e05147d49af7bed31a6e99",
        urls=(
            # 实测 12.5 MB/s，同一份文件（Content-Range 总长 176069933）
            "https://hf-mirror.com/jellybox/isnet-anime/resolve/main/isnet-anime_1024.onnx",
            # 上游；直连 0.25~1.3 MB/s，速度不稳，接口正常
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-anime.onnx",
            # 官方 HF。国内多数线路不可达，留给能直连的用户兜底
            "https://huggingface.co/jellybox/isnet-anime/resolve/main/isnet-anime_1024.onnx",
        ),
        required=True,
    ),
    Asset(
        key="realesrgan",
        label="超分组件",
        dest="tools/realesrgan",
        probe="tools/realesrgan/realesrgan-ncnn-vulkan.exe",
        size=45_500_000,
        # 两个源的包实测字节完全一致，共用一个指纹与断点续传缓存
        sha256="abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d",
        urls=(
            "https://gitee.com/DXFB/Real-ESRGAN-ncnn-vulkan/releases/download/v0.2.0/"
            "realesrgan-ncnn-vulkan-20220424-windows.zip",
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
            "realesrgan-ncnn-vulkan-20220424-windows.zip",
        ),
        required=True,
        members=(
            ("realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan.exe"),
            ("vcomp140.dll", "vcomp140.dll"),
            ("models/realesr-animevideov3-x2.bin", "models/realesr-animevideov3-x2.bin"),
            ("models/realesr-animevideov3-x2.param", "models/realesr-animevideov3-x2.param"),
            ("models/realesr-animevideov3-x3.bin", "models/realesr-animevideov3-x3.bin"),
            ("models/realesr-animevideov3-x3.param", "models/realesr-animevideov3-x3.param"),
            ("models/realesr-animevideov3-x4.bin", "models/realesr-animevideov3-x4.bin"),
            ("models/realesr-animevideov3-x4.param", "models/realesr-animevideov3-x4.param"),
        ),
    ),
    Asset(
        key="spineconv",
        label="Spine 转换器",
        dest="tools/spineconv",
        base=RAW + "/tools/spineconv",
        # raw.githubusercontent 在国内会被中途 RST（实测 11MB 分四路并发，跑到
        # 7.3MB 那个 exe 的尾巴上断过一次）。这两个转发代理把同一个 raw 地址包一层，
        # 拿到的字节完全一样，所以 sha256 不用改。jsdelivr 试过，超过 ~20MB 直接 403，
        # 这组文件里最大的 7.3MB 虽然没超，但它现在对本仓库整体返回 403，不能用。
        mirrors=(
            "https://gh-proxy.com/" + RAW + "/tools/spineconv",
            "https://ghproxy.net/" + RAW + "/tools/spineconv",
        ),
        sha256="",
        urls=(),
        size=sum(z for _, z, _ in _index("spineconv")),
        files=_index("spineconv"),
        required=True,
    ),
)

BY_KEY = {a.key: a for a in ASSETS}


def present(asset: Asset) -> bool:
    """这个资源在不在。只看文件在不在、大小对不对，不算指纹（168MB 算一遍要小半秒）。"""
    if asset.files:
        root = runtime.bundle_root() / asset.dest
        for rel, size, _ in asset.files:
            try:
                if (root / rel).stat().st_size != size:
                    return False
            except OSError:
                return False
        return True
    return asset.marker.is_file()


def missing(only_required: bool = False) -> list:
    return [a for a in ASSETS if not present(a) and (a.required or not only_required)]


def status() -> dict:
    """给界面用的清单。"""
    return {"assets": [
        {"key": a.key, "label": a.label, "size": a.size,
         "required": a.required, "present": present(a)}
        for a in ASSETS
    ]}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _cache_dir() -> Path:
    d = runtime.bundle_root() / ".download"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _partial(asset: Asset) -> Path:
    return _cache_dir() / f"{asset.key}.part"


def partial_bytes(asset: Asset) -> int:
    p = _partial(asset)
    return p.stat().st_size if p.is_file() else 0


def _opener():
    """默认直连，不走系统/env 代理。

    实测（2026-11-06）：这台机器设了 HTTP(S)_PROXY 时 github.com 走代理隧道返回 502，
    直连反而 1.6 秒就拿到 206。需要代理的环境用 MDSTUDIO_PROXY 显式指定。
    """
    proxy = os.environ.get("MDSTUDIO_PROXY", "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# 分块大小。取消是靠杀进程实现的，被杀时可能留下写了一半的块，
# 所以续传前要把残片截到整块边界，拼接才不会错位。
CHUNK = 1 << 18


def _download_one(url: str, dest: Path, cancel, on_progress) -> None:
    """带 Range 续传地把 url 写到 dest。已存在的部分会被续上。"""
    have = dest.stat().st_size if dest.is_file() else 0
    if have % CHUNK:
        have -= have % CHUNK
        try:
            with open(dest, "r+b") as f:
                f.truncate(have)
        except OSError:
            have = 0
    req = urllib.request.Request(url, headers={"User-Agent": "MDStudio"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with _opener().open(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0) + have
        mode = "ab" if have and r.status == 206 else "wb"
        if mode == "wb":
            have = 0
        done = have
        t0 = time.time()
        with open(dest, mode) as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise _Canceled()
                block = r.read(CHUNK)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if on_progress:
                    speed = done / max(0.001, time.time() - t0)
                    on_progress(done, total, speed)


class _Canceled(Exception):
    pass


# 网络层的失败多半是瞬时的（对端 RST、代理抖动、杀软插队扫描新下载的 exe）。
# 这些异常本身不可怕，可怕的是不留重试：11MB 的组件跑到最后一个文件断掉，
# 用户只能看到一行英文异常名，文件其实已经写了一半。
# 同源重试时残片可以续传；换源时因为镜像字节一致也能续（不一致会被 sha256 拦下）。
_NET_ERRORS = (urllib.error.URLError, OSError, ValueError, http.client.HTTPException)

SINGLE_ATTEMPTS = 3         # 同一个源重试几次
GROUP_ATTEMPTS = 4          # 分组下载每个文件总共试几次，每次换下一个源
BACKOFF = (1.0, 2.0, 4.0)


def _why(e) -> str:
    """把网络异常翻成一句人话。弹窗里不出现 ConnectionResetError 这种类型名。"""
    # Windows 的 socket 错误文本自带句号与 [WinError N] 前缀，直接嵌进句子会很脏
    def clean(s: str) -> str:
        return re.sub(r"\s*\[WinError \d+\]", "", s).strip().rstrip("。.").strip()

    if isinstance(e, urllib.error.HTTPError):
        return tr("the server returned %s") % e.code
    if isinstance(e, urllib.error.URLError):
        r = getattr(e, "reason", e)
        if isinstance(r, (socket.timeout, TimeoutError)):
            return tr("the connection timed out")
        if isinstance(r, ConnectionResetError):
            return tr("the connection was cut off by the server")
        return clean(str(r)) or tr("the connection could not be opened")
    if isinstance(e, ConnectionResetError) or getattr(e, "winerror", None) == 10054:
        return tr("the connection was cut off by the server")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return tr("the connection timed out")
    return clean(str(e)) or type(e).__name__


# 多源时先各取一小段比速度。10MB 占 168MB 模型的 6%，足够跑过 TCP 慢启动，
# 让测出来的吞吐接近稳态；再小容易被前一两秒的爬坡带偏。
PROBE_BYTES = 10 << 20
PROBE_LIMIT = 6.0       # 单源最多比这么久（秒）
PROBE_GRACE = 1.0       # 有源跑满后再给别人一点时间


def _probe_sources(urls, cancel, probe_bytes=PROBE_BYTES):
    """并发各取前 probe_bytes，挑吞吐最高的那个源。

    返回 (url, 已下到的开头字节)。中途放弃的源留下的数据直接丢掉——
    不同源拼起来是另一个错误来源，不值得省那几 MB。
    """
    import threading

    res = {}
    first = threading.Event()

    def one(i, url):
        t0, buf = time.time(), bytearray()
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "MDStudio",
                              "Range": f"bytes=0-{probe_bytes - 1}"})
            with _opener().open(req, timeout=10) as r:
                while len(buf) < probe_bytes:
                    if cancel is not None and cancel.is_set():
                        raise _Canceled()
                    if time.time() - t0 > PROBE_LIMIT:
                        break
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    buf += chunk
        except _Canceled:
            raise
        except Exception:
            pass
        res[i] = (len(buf) / max(0.001, time.time() - t0), bytes(buf), url)
        if len(buf) >= probe_bytes:
            first.set()

    threads = [threading.Thread(target=one, args=(i, u), daemon=True)
               for i, u in enumerate(urls)]
    for t in threads:
        t.start()
    first.wait(PROBE_LIMIT)
    if first.is_set():
        time.sleep(PROBE_GRACE)
    best = max(res.values(), key=lambda x: x[0]) if res else None
    if not best or not best[1]:
        return None, b""
    return best[2], best[1]


def _fetch_group(asset: Asset, cancel, on_progress) -> None:
    """逐文件下载一组素材（超框图片、Spine 转换器这种）。

    并发几个连接，按"已完成的字节"报进度；已经落盘的文件保留，下次接着补。

    网络异常会重试，每次换下一个镜像源。换源时**保留残片**用 Range 续传：镜像的
    字节一致，真不一致时末尾的 sha256 会拦下来重下。11MB 分成四路并发、最长的
    那个 exe 要跑二十多秒，中途被 RST 是常态，不重试就得让用户从头再点一遍。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from . import progress as _progress
    from .errors import AssetInterrupted, MDStudioError

    total = sum(z for _, z, _ in asset.files)
    done = 0
    shown = 0       # 报出去的最大字节：换源重下时真字节会退回去，条不该跟着回跳
    live = {}       # rel -> 该文件已写入的字节（含续传起点），逐字节进度合并用
    lock = threading.Lock()

    def publish(force_lower=False):
        """报一次总进度。

        默认只增不减。某个文件下到 100% 时被 RST，重试会让它的字节数从"在途"里
        退回去，进度条跟着从 100% 掉到 34%（2026-11-12 用户实测，看着像重下）。
        只有校验失败、那份文件真的作废了才允许回落。
        """
        nonlocal shown
        with lock:
            cur = min(done + sum(live.values()), total)
            if not force_lower and cur < shown:
                cur = shown
            shown = cur
        if on_progress:
            on_progress(cur, total, 0)

    bases = list(asset.bases or (asset.base,))
    # 多源时先并发测速, 从最快的源起步。raw 和代理的速度随网络状态换着来
    # (2026-11-11 实测: 同一天 raw 从 380KB/s 变成直接超时, 代理反而能用),
    # 赌主源不如花两三秒探一轮。探测量给 2MB: 组件总共才十几 MB, 探 10MB 太贵。
    if len(bases) > 1:
        _progress.step(tr("picking the fastest mirror"))
        best, _head = _probe_sources(bases, cancel, probe_bytes=2 << 20)
        if best:
            bases = [best] + [b for b in bases if b != best]

    def one(rel: str, size: int, sha: str) -> None:
        nonlocal done
        part = _cache_dir() / asset.key / (rel + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)

        def per_file(n, _t, _s):
            # 大 exe 在慢源上要下几十秒，只在"文件完成"时报进度的话，
            # 进度条会长时间纹丝不动（2026-11-11 用户实测：卡 100% 的观感）。
            # 逐字节并进总进度：已完成文件的字节 + 在途文件的字节。
            with lock:
                live[rel] = n
            publish()

        last = None
        for attempt in range(GROUP_ATTEMPTS):
            if cancel is not None and cancel.is_set():
                raise _Canceled()
            base = bases[attempt % len(bases)]
            url = base + "/" + urllib.parse.quote(rel)
            if attempt:
                # 换源不再删残片：镜像的字节一致，Range 能接着往下走；真不一致时
                # 末尾的 sha256 会把它拦下来，大不了多走一轮。删残片等于把几十兆
                # 的成果扔掉，进度还会倒着跳。
                _progress.step(tr("retrying %s (%s)") % (rel, last or "?"))
            try:
                _download_one(url, part, cancel, per_file)
                if sha:
                    _progress.step(tr("verifying %s") % rel)
                if sha and _sha256(part) != sha:
                    # 内容是坏的（半截文件也会走到这），删掉重来
                    part.unlink(missing_ok=True)
                    with lock:
                        live.pop(rel, None)
                    publish(force_lower=True)      # 这份文件作废，进度退回去
                    last = tr("checksum mismatch")
                else:
                    out = runtime.bundle_root() / asset.dest / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(part, out)
                    with lock:
                        live.pop(rel, None)
                        done += size
                    publish()
                    return
            except _Canceled:
                raise
            except _NET_ERRORS as e:
                last = _why(e)
            except MDStudioError:
                raise
            if attempt + 1 < GROUP_ATTEMPTS:
                # 退避期间也要能取消，别让用户等了七八秒才响应
                waited = 0.0
                step = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                while waited < step:
                    if cancel is not None and cancel.is_set():
                        raise _Canceled()
                    time.sleep(0.1)
                    waited += 0.1
        # 分组下载要说清是哪个文件没下来，不然用户不知道该不该重试。
        # 这里只报原因，「已下载的部分会保留」由界面放进提示条说，正文不重复。
        detail = f"{rel}: {last}" if last else rel
        raise AssetInterrupted(tr("download was interrupted: %s") % detail)

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(asset.files)))) as ex:
        futures = [ex.submit(one, rel, size, sha) for rel, size, sha in asset.files]
        for f in as_completed(futures):
            f.result()


def fetch(asset: Asset, cancel=None, on_progress=None) -> None:
    """下载并安装一个资源。失败抛 MDStudioError，已下的部分保留以便续传。

    多个源时先并发测速（见 _probe_sources），再从最快的那个接着下。
    指纹对不上也换下一个源，因为镜像上的同名文件未必是同一份。
    """
    from .errors import AssetInterrupted, MDStudioError

    if asset.files:
        _fetch_group(asset, cancel, on_progress)
        return

    part = _partial(asset)
    urls = list(asset.urls)
    order = urls
    prefix = b""
    if len(urls) > 1 and asset.size > PROBE_BYTES:
        # 测速要 6 秒左右，这期间没有任何字节回报，界面会像卡住。先报一声阶段
        from . import progress as _progress
        _progress.step(tr("picking the fastest mirror"))
        best, prefix = _probe_sources(urls, cancel)
        if best:
            order = [best] + [u for u in urls if u != best]
    if prefix:
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(prefix)

    last = None
    ok = False
    for i, url in enumerate(order):
        for attempt in range(SINGLE_ATTEMPTS):
            try:
                _download_one(url, part, cancel, on_progress)
            except _Canceled:
                raise
            except _NET_ERRORS as e:
                last = _why(e)
                # 同一源续传，退避后再来一次
                if attempt + 1 < SINGLE_ATTEMPTS:
                    waited = 0.0
                    step = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                    while waited < step:
                        if cancel is not None and cancel.is_set():
                            raise _Canceled()
                        time.sleep(0.1)
                        waited += 0.1
                    continue
                break
            if not part.is_file():
                last = tr("nothing was written")
                break
            got = _sha256(part)
            if asset.sha256 and got != asset.sha256:
                # 来源不是同一份文件，删掉换下一个源
                part.unlink(missing_ok=True)
                last = tr("checksum mismatch (got %s)") % got[:12]
                break
            ok = True
            break
        if ok:
            break
        if part.is_file():
            # 换了源就不能拿上一个源的残片续传，拼起来是坏文件
            part.unlink(missing_ok=True)
    if not ok:
        raise AssetInterrupted(tr("download was interrupted: %s")
                               % (last or tr("no data came through")))

    target = asset.target
    if asset.members:
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(part) as z:
            names = set(z.namelist())
            for src, rel in asset.members:
                if src not in names:
                    raise MDStudioError(tr("%s is missing from the package") % src)
                out = target / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(src) as fi, open(out, "wb") as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
        part.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)


def ensure(key: str, cancel=None, on_progress=None) -> None:
    a = BY_KEY.get(key)
    if a is None:
        raise KeyError(key)
    if present(a):
        return
    fetch(a, cancel=cancel, on_progress=on_progress)
