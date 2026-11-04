"""manifest_tcg 的 SimpleCrypto: .NET System.Random 当 XOR 密钥流"""
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


def _keystream(key: str, n: int) -> np.ndarray:
    rng, ks = _STREAMS.get(key) or (NetRandom(sum(key.encode("utf-8"))), np.zeros(0, np.uint8))
    if len(ks) < n:
        extra = np.fromiter((rng.next_byte() for _ in range(n - len(ks) + 65536)), np.uint8)
        ks = np.concatenate([ks, extra])
        _STREAMS[key] = (rng, ks)
    return ks[:n]


def crypt(data: bytes, key: str = "manifest") -> bytes:
    # XOR, 加密解密同一个函数. seed = key 字节和 ("manifest" -> 855)
    a = np.frombuffer(data, np.uint8)
    return (a ^ _keystream(key, len(a))).tobytes()
