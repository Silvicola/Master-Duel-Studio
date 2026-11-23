"""manifest_tcg: 游戏真正用的依赖表 (bundle 里自带的 m_Dependencies 游戏根本不看)"""
import json

from mdstudio.core.crc import get_crc
from mdstudio.core.crypto import crypt
from mdstudio.errors import AssetMissing
from mdstudio.i18n import tr

MANIFEST = "manifest_tcg"


def decode(raw: bytes) -> dict:
    return json.loads(crypt(raw).decode("utf-8"))


def encode(data: dict) -> bytes:
    # 紧凑 json, 跟 JsonUtility 一个格式
    return crypt(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class Catalog:
    def __init__(self, game):
        self.game = game
        self.path = game.find(get_crc(MANIFEST))
        if not self.path:
            raise AssetMissing(tr("manifest_tcg not found in the active LocalData group"))
        self.data = decode(self.path.read_bytes())
        self._idx = {e["assetName"]: e for e in self.data["informations"]}
        self._loose = None

    def find_actual(self, name):
        """按名字找条目，容忍官方把名字写歪。

        实测 12 张卡的 prefab 条目叫 `HighEnd_HD/P7302 `（尾部多一个空格），
        按字面拼出来的路径查不到依赖，生成时这一档被静默跳过。这里先按原样找，
        找不到再用去掉尾部空格的索引找一次，返回磁盘上真实的名字。
        """
        if name in self._idx:
            return name
        if self._loose is None:
            self._loose = {}
            for n in self._idx:
                if n != n.rstrip():
                    self._loose.setdefault(n.rstrip(), n)
        return self._loose.get(name.rstrip())

    def prefab_candidates(self, base: str, quality: str, mrk: int) -> list:
        """某一档可能叫这个名字的所有 prefab，按优先级排。

        官方有几种写歪的方式：
        - 尾部带空格：`HighEnd_HD/P7302 `（7302、16841 这批）
        - 多一层缩放目录：`HighEnd_HD/0.71/P18166`、`SD/0.5/P7302`

        同一张卡可能同时存在「主 prefab」和「带图集的 prefab」，前者没挂 atlas，
        所以要按候选顺序试，第一个能查到本档图集的才算数（见 ops/cutin.asset_paths）。
        """
        want = f"P{int(mrk)}"
        out = []
        hit = self.find_actual(f"{base}/{quality}/{want}")
        if hit:
            out.append(hit)
        for n in self._idx:
            s = n.rstrip()
            if not (s.startswith(base + "/") and s.endswith("/" + want)):
                continue
            mid = s[len(base) + 1:-(len(want) + 1)]
            if mid.startswith(quality + "/") and n not in out:
                out.append(n)
        return out

    def __contains__(self, name):
        return name in self._idx

    def names(self):
        return self._idx.keys()

    def deps(self, name):
        e = self._idx.get(name)
        return None if e is None else list(e["dependencies"])

    def set(self, name, deps):
        """返回之前的 deps (新条目返回 None), 给 journal 记"""
        e = self._idx.get(name)
        if e is None:
            e = {"assetName": name, "dependencies": list(deps)}
            self.data["informations"].append(e)
            self._idx[name] = e
            return None
        prev = list(e["dependencies"])
        e["dependencies"] = list(deps)
        return prev

    def remove(self, name):
        e = self._idx.pop(name, None)
        if e is not None:
            self.data["informations"].remove(e)

    def missing(self, name):
        """name 的依赖里, 硬盘上不存在的"""
        return [d for d in (self.deps(name) or []) if not self.game.find_logical(d)]

    def save(self, ctx):
        ctx.write(self.path, encode(self.data))
