"""manifest_tcg 的 SimpleCrypto: .NET System.Random 当 XOR 密钥流"""
import hashlib
import os
import threading

import numpy as np

_MBIG = 2147483647
_MSEED = 161803398


class NetRandom:
    """Knuth subtractive, 抄 .NET 源码. 唯一区别 inextp 初始 31 (原版 21)"""

    def __init__(self, seed: int, inextp: int = 31):
        sa = [0] * 56
        mj = _MSEED - abs(seed)
        sa[55] = mj
        mk = 1
        for i in range(1, 55):
            ii = (21 * i) % 55
            sa[ii] = mk
            mk = mj - mk
            if mk < 0:
                mk += _MBIG
            mj = sa[ii]
        for _ in range(4):
            for i in range(1, 56):
                sa[i] -= sa[1 + (i + 30) % 55]
                if sa[i] < 0:
                    sa[i] += _MBIG
        self._sa = sa
        self._inext = 0
        self._inextp = inextp

    def sample(self) -> int:
        self._inext = 1 if self._inext + 1 >= 56 else self._inext + 1
        self._inextp = 1 if self._inextp + 1 >= 56 else self._inextp + 1
        r = self._sa[self._inext] - self._sa[self._inextp]
        if r < 0:
            r += _MBIG
        self._sa[self._inext] = r
        return r

    def next_byte(self) -> int:
        return int(self.sample() * (1.0 / _MBIG) * 256.0) & 0xFF


_STREAMS = {}   # key -> (NetRandom, 已生成的密钥流). manifest 4.7MB, 纯 python 逐字节太慢, 缓存起来
_STREAM_LOCK = threading.Lock()


def _cache_path(key):
    from mdstudio import runtime

    tag = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return runtime.data_dir() / f"keystream_{tag}.bin"


def _keystream(key: str, n: int) -> np.ndarray:
    """只依赖 key 的确定性密钥流。

    生成是纯 Python 逐字节循环，4.1MB 要 1.9 秒，所以落盘复用。
    磁盘缓存里只有字节、没有 rng 状态：一旦需要比缓存更长的流，必须**从头**
    重新生成，不能把新字节接到旧序列后面（那样第 len(ks) 位会错）。
    """
    with _STREAM_LOCK:
        rng, ks = _STREAMS.get(key) or (None, np.zeros(0, np.uint8))
        if len(ks) >= n:
            return ks[:n]

        p = _cache_path(key)
        disk = None
        try:
            disk = np.frombuffer(p.read_bytes(), np.uint8)
        except (OSError, ValueError):
            pass
        if disk is not None and len(disk) >= n:
            # 只读视图，XOR 不改它，可以直接用
            _STREAMS[key] = (None, disk)
            return disk[:n]

        if disk is not None and len(disk) > len(ks):
            ks, rng = disk, None       # 取更长的那份，但 rng 状态作废

        if rng is not None and len(ks) > 0:
            # 同进程内续着 rng 往下补，最便宜
            need = n - len(ks) + 65536
            extra = np.fromiter((rng.next_byte() for _ in range(need)), np.uint8)
            ks = np.concatenate([ks, extra])
        else:
            # 首次、或从磁盘读回后还要变长：整段重算
            rng = NetRandom(sum(key.encode("utf-8")))
            ks = np.fromiter((rng.next_byte() for _ in range(n + 65536)), np.uint8)

        _STREAMS[key] = (rng, ks)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(ks.tobytes())
            os.replace(tmp, p)
        except OSError:
            pass       # 缓存写不了不影响正确性，下次再生成就是
        return ks[:n]


def crypt(data: bytes, key: str = "manifest") -> bytes:
    # XOR, 加密解密同一个函数. seed = key 字节和 ("manifest" -> 855)
    a = np.frombuffer(data, np.uint8)
    return (a ^ _keystream(key, len(a))).tobytes()
