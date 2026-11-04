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
