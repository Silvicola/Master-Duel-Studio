"""卡名 <-> 卡 id, 直接从游戏自己的数据读, 不依赖外部数据库

数据:
  Card/Data/<16hex>/<lang>/CARD_Name   卡名大 blob (加密)
  Card/Data/<16hex>/<lang>/CARD_Indx   每张卡在 blob 里的偏移 (加密)
  Card/ScriptableObjects/<lang>/CardPictureFontSetting   cardid 列表, 跟名字顺序一一对应

<16hex> 这个目录名会随版本变, manifest 里也没有. 先用缓存的, 失效了再扫 bundle 找.
"""
import json
import logging
import os
import re
import unicodedata
import zlib

import numpy as np
import UnityPy

from mdstudio.core.crc import get_crc
from mdstudio.core.journal import home_dir
from mdstudio.errors import AssetMissing
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")

_CONST = 0x23D          # 公式里的固定常数, 不是 key
_KEY_LIMIT = 0x400      # key 每次大更新会变, 历史最大 0x25F
_SIZE_HINT = 116419     # CARD_Name bundle 大概这么大, 扫的时候按接近程度排序
_CONTAINER_RE = re.compile(rb"card/data/([0-9a-f]{16})/([a-z]{2}-[a-z]{2,4})/card_name")


def _keystream(n: int, key: int) -> np.ndarray:
    i = np.arange(n, dtype=np.int64)
    return ((((i + key + _CONST) * key) ^ (i % 7)) & 0xFF).astype(np.uint8)


def text_decrypt(enc: bytes, key: int) -> bytes:
    a = np.frombuffer(enc, np.uint8)
    return zlib.decompress((a ^ _keystream(len(a), key)).tobytes())


def find_key(enc: bytes) -> int:
    # 只解前 512 字节试 zlib, 错的 key 基本第一块就报错, 不用每个 key 都整段解
    head = np.frombuffer(enc[:512], np.uint8)
    for key in range(_KEY_LIMIT):
        try:
            if zlib.decompressobj().decompress((head ^ _keystream(len(head), key)).tobytes()):
                text_decrypt(enc, key)
                return key
        except zlib.error:
            continue
    raise AssetMissing(tr("card text key not found (game format changed?)"))


def _textasset(data: bytes) -> bytes:
    for o in UnityPy.load(data).objects:
        if o.type.name == "TextAsset":
            s = o.read().m_Script
            return s.encode("utf-8", "surrogateescape") if isinstance(s, str) else bytes(s)
    raise AssetMissing(tr("no TextAsset in bundle"))


def _split(indx: bytes, blob: bytes) -> list:
    offs = [int.from_bytes(indx[i:i + 4], "little") for i in range(0, len(indx) - 3, 8)][1:]
    return [blob[a:b].decode("utf-8", "replace").rstrip("\x00") for a, b in zip(offs, offs[1:])]


def fold(s: str) -> str:
    """搜索用: 去重音 + casefold + 只留字母数字, "Despian" 也能搜到 "Despia"""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^\w]+", " ", s).strip()


_REAL_CACHE = "real_ids.json"

# 卡名表里名字就叫「未知」的空号（游戏里不是真卡，无法使用/修改）：3001、21219。
# 只排名字 **完全等于** 它们的号；名字里含「未知」的真卡（如「未知青蛙」「未知同步士」）
# 不受影响。再用真卡号集合兜底，万一哪天官方真出了个叫「未知」的卡也不会被误删。
_HIDDEN_NAMES = {"未知", "unknown", "Unknown"}


def real_card_ids(game) -> set:
    """游戏认可的真卡号: card_rarity_asset 的键

    游戏内部另有一套"占位副本号", 名字和卡图跟真卡一模一样, 但渲染时不走它,
    改它等于白改. 这些号不在稀有度表里; 异画的两个号都在表里. 所以这张表
    能把占位号和异画分开, 不会误伤异画.

    按 data.unity3d 的大小 + 修改时间缓存, 游戏更新后自动重建.
    """
    p = game.dir / "masterduel_Data" / "data.unity3d"
    if not p.is_file():
        return set()
    fp = [p.stat().st_size, int(p.stat().st_mtime)]
    cache = home_dir() / game.install_id / _REAL_CACHE
    if cache.is_file():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("fp") == fp:
                return {int(c) for c in d["ids"]}
        except (OSError, ValueError, KeyError):
            pass
    ids = set()
    for o in UnityPy.load(p.read_bytes()).objects:
        if o.type.name != "TextAsset":
            continue
        try:
            d = o.read()
        except Exception:
            continue
        if str(d.m_Name) != "card_rarity_asset":
            continue
        s = d.m_Script
        raw = s.encode("utf-8", "surrogateescape") if isinstance(s, str) else bytes(s)
        # 每条 4 字节: 卡号(uint16) + 稀有度(uint16), 只要卡号
        ids = {int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw) - 3, 4)}
        break
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fp": fp, "ids": sorted(ids)}), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError as e:
        log.warning(tr("can't save the real card id cache: %s"), e)
    return ids


class CardDB:
    def __init__(self, game, lang: str = None):
        """lang=None: 用游戏当前的语言 (盘上只有那一份)"""
        self.game = game
        self.lang = lang
        self._auto = lang is None
        self.names = {}           # id -> name
        self._folded = []         # [(id, folded name)]
        self._real = None         # 真卡号集合, 第一次问的时候才读
        self._toks = None         # 衍生物号集合, 第一次问的时候才从 CARD_Prop 解
        self._cache = home_dir() / game.install_id / f"cards_{lang or 'auto'}.json"

    # ---- 定位 ----
    def _paths(self, folder):
        return {"name": f"Card/Data/{folder}/{self.lang}/CARD_Name",
                "indx": f"Card/Data/{folder}/{self.lang}/CARD_Indx",
                "font": f"Card/ScriptableObjects/{self.lang}/CardPictureFontSetting"}

    def _ok(self, folder):
        return bool(folder and self.lang) and all(self.game.find_logical(p) for p in self._paths(folder).values())

    def _accept(self, folder, lang_lower):
        """container 里全是小写, crc 要原始大小写. 试几种写法, 能对上 crc 的就是"""
        a, _, b = lang_lower.partition("-")
        for cand in (f"{a}-{b.upper()}", f"{a}-{b.title()}", lang_lower):
            if self.lang and cand.lower() != self.lang.lower():
                continue
            keep, self.lang = self.lang, cand
            if self._ok(folder):
                return True
            self.lang = keep
        return False

    def _probe_folder(self):
        files = [p for d in self.game.root.iterdir() if d.is_dir() for p in d.iterdir()]
        files.sort(key=lambda p: abs(p.stat().st_size - _SIZE_HINT))
        for p in files[:6000]:
            data = p.read_bytes()
            # bundle 头部没压缩的情况能直接 grep 到, 压缩的就只能 load
            m = _CONTAINER_RE.search(data[:4096])
            if not m:
                try:
                    env = UnityPy.load(data)
                    names = [c for o in env.objects if o.type.name == "AssetBundle"
                             for c, _ in o.read_typetree().get("m_Container", [])]
                except Exception:
                    continue
                m = next((x for x in (_CONTAINER_RE.search(n.encode()) for n in names) if x), None)
            if m and self._accept(m.group(1).decode(), m.group(2).decode()):
                return m.group(1).decode()
        return None

    # ---- 加载 ----
    def _fingerprint(self, folder):
        fp = {}
        for k, lp in self._paths(folder).items():
            p = self.game.find_logical(lp)
            fp[k] = [p.name, p.stat().st_size, int(p.stat().st_mtime)]
        return fp

    def load(self) -> "CardDB":
        cached = None
        if self._cache.is_file():
            try:
                cached = json.loads(self._cache.read_text(encoding="utf-8"))
            except ValueError:
                cached = None
        folder = cached.get("folder") if cached else None
        if cached and not self.lang:
            self.lang = cached.get("lang")
        if self._ok(folder) and cached.get("fp") == self._fingerprint(folder):
            self._set({int(k): v for k, v in cached["names"].items()})
            return self
        if not self._ok(folder):
            log.info(tr("locating card name data (first run after an update, takes a moment)…"))
            if self._auto:
                self.lang = None   # 用户可能换了游戏语言, 缓存里的语言不作数
            folder = self._probe_folder()
            if not self._ok(folder):
                # 游戏只下载当前设置的语言那一份
                raise AssetMissing(tr("no card names for %s on disk. The game only keeps the language "
                                      "it's set to; use that one (--lang), or open the game once.")
                                   % (self.lang or tr("any language")))
        paths = {k: self.game.find_logical(v) for k, v in self._paths(folder).items()}
        enc_name = _textasset(paths["name"].read_bytes())
        key = find_key(enc_name)
        names = _split(text_decrypt(_textasset(paths["indx"].read_bytes()), key), text_decrypt(enc_name, key))
        ids = []
        for o in UnityPy.load(paths["font"].read_bytes()).objects:
            if o.type.name == "MonoBehaviour":
                tt = o.read_typetree()
                if "m_CardidNormalFontSizePairDatas" in tt:
                    ids = [e["cardid"] for e in tt["m_CardidNormalFontSizePairDatas"]]
                    break
        if not ids:
            raise AssetMissing(tr("CardPictureFontSetting has no card id list"))
        # 两个列表顺序一一对应 (CardTextStudio 那边验证过)
        id2name = {cid: nm.strip() for cid, nm in zip(ids, names) if cid and cid > 0 and nm.strip()}
        meta = {"folder": folder, "lang": self.lang, "fp": self._fingerprint(folder), "key": key}
        try:
            # 缓存坏了也只是下次多扫一遍, 写不进去不影响这次用
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({**meta, "names": id2name}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._cache)
        except OSError as e:
            log.warning(tr("can't save the card name cache: %s"), e)
        self._set(id2name)
        log.info(tr("%d card names loaded (%s)"), len(id2name), self.lang)
        return self

    def _set(self, id2name):
        self.names = id2name
        self._folded = [(cid, fold(nm)) for cid, nm in id2name.items()]
        # 名字 -> id 索引. 以前 same_name 每次扫全表, 搜索 300 条结果 = 300 x 14k, 打一个字卡半秒
        self._by_name = {}
        for cid, nm in id2name.items():
            self._by_name.setdefault(nm, []).append(cid)

    # ---- 查询 ----
    def name(self, cid: int) -> str:
        return self.names.get(int(cid), "")

    def same_name(self, cid: int) -> list:
        """同名的所有 id (异画/不同印刷). cut-in gate 只认其中一个 id, 查的时候要全看"""
        nm = self.names.get(int(cid))
        return sorted(self._by_name[nm]) if nm else [int(cid)]

    def placeholder_of(self, cid: int) -> int:
        """占位副本号返回游戏真正渲染的那个号, 其余(含异画)返回 0

        判据: 自己不在真卡名单, 而同名的另一个号在. 异画两个号都在名单里,
        所以选异画不会触发. 没有同名兄弟的孤号也不动, 免得把衍生物之类的误判.
        """
        cid = int(cid)
        nm = self.names.get(cid, "")
        if not nm:
            return 0
        if self._real is None:
            self._real = real_card_ids(self.game)
        if not self._real or cid in self._real:
            return 0
        for sib in self._by_name.get(nm, ()):
            if sib != cid and sib in self._real:
                return int(sib)
        return 0

    def _visible(self, cid: int) -> bool:
        """这张号该不该出现在列表/搜索里。

        排掉两类不是真卡的东西：
          - 名字叫「未知」的空号（真卡同名才留，防误杀）；
          - 不存在的隐藏卡：CARD_Prop 类型 10/49，但名字不是「衍生物」（如 22591
            「妖精的王子大人」，只在 #22542 的效果里替换卡名）。真衍生物（名字带
            「衍生物/Token」）保留，它们能被召唤。
        """
        cid = int(cid)
        nm = self.names.get(cid, "")
        if nm in _HIDDEN_NAMES:
            if self._real is None:
                self._real = real_card_ids(self.game)
            return cid in self._real
        toks = self._token_ids()
        if toks and cid in toks and not ("衍生物" in nm or "Token" in nm or "token" in nm):
            return False
        return True

    def visible_names(self) -> dict:
        """过滤掉「未知」空号后的 id -> name。给「列出全部卡」这种全量遍历用，
        保证和 search() 一样的可见性规则（否则空搜索列表里还会冒出 3001/21219）。"""
        return {cid: nm for cid, nm in self.names.items() if self._visible(cid)}

    def _token_ids(self) -> set:
        """类型 10 / 49 的卡号集合（衍生物，以及少数「不存在的隐藏卡」），从游戏 CARD_Prop 解。

        每张卡 8 字节，第 3 字节（bits 18-23）是卡类型；实测（用 3900/3915 两个衍生物
        反推验证）类型 = 该字节 & 0x3F，10 = 衍生物（非效果）、49 = 衍生物/调整。
        注意游戏把这类号归得比较粗：它同时收进了「妖精的王子大人」(22591) 这种只用于
        效果替换卡名、实际不存在的隐藏卡，所以这里叫「不可当实卡处理」的号更准确。
        只在第一次问时解一遍，缓存在实例上；解不出来返回空集，由调用方走兜底。
        """
        if getattr(self, "_toks", None) is not None:
            return self._toks
        self._toks = set()
        cache = getattr(self, "_cache", None)
        try:
            meta = json.loads(cache.read_text(encoding="utf-8"))
            folder = meta.get("folder")
            lang = meta.get("lang") or self.lang
        except (AttributeError, OSError, ValueError):
            return self._toks
        if not (folder and lang):
            return self._toks
        p = self.game.find_logical(f"Card/Data/{folder}/{lang}/CARD_Prop")
        if not p:
            return self._toks
        try:
            enc = _textasset(p.read_bytes())
            dec = text_decrypt(enc, find_key(enc))
        except Exception:      # 解不开就当没有，别让搜索整个挂掉
            return self._toks
        toks = set()
        for i in range(0, len(dec) - 7, 8):
            if (dec[i + 2] & 0x3F) in (10, 49):
                toks.add(dec[i] | (dec[i + 1] << 8))
        self._toks = toks
        return toks

    def is_unreleased(self, cid: int) -> bool:
        """这张卡是不是「新卡还没上线」。

        判据：在卡名表里有名字，但不在稀有度表（card_rarity_asset）里，也就是包里开不到、
        也合成不了。排除三类不属于「未上线」的号：
          - 占位卡（有同名真卡兄弟，内容其实已上线）；
          - 衍生物与「不存在的隐藏卡」（CARD_Prop 类型位 10/49，如 22591 妖精的王子大人；
            解不到数据时退回「名字带衍生物或 39xx 段」）；
          - 「未知」空号。
        游戏更新后稀有度表会重算，新卡一旦上线这个判断自动变 False，不用改代码。
        """
        cid = int(cid)
        nm = self.names.get(cid)
        if not nm or nm in _HIDDEN_NAMES:
            return False
        if self._real is None:
            self._real = real_card_ids(self.game)
        if not self._real or cid in self._real:
            return False
        if self.placeholder_of(cid):
            return False
        toks = self._token_ids()
        if toks:
            if cid in toks:
                return False
        elif "衍生物" in nm or "Token" in nm or "token" in nm or 3900 <= cid < 4000:
            return False
        return True

    def search(self, query: str, limit: int = 50) -> list:
        """数字 = 按 id; 否则所有词都要出现在名字里. 排序: 完全一致 > 开头一致 > 词开头 > 其他"""
        q = query.strip()
        if not q:
            return []
        self.last_fuzzy = False
        if q.isascii() and q.isdigit():
            hits = [cid for cid in self.names
                    if str(cid).startswith(q) and self._visible(cid)]
            hits.sort(key=lambda c: (str(c) != q, c))
            return [(c, self.names[c]) for c in hits[:limit]]
        fq = fold(q)
        words = fq.split()
        scored = []
        for cid, fn in self._folded:
            if all(w in fn for w in words):
                rank = 0 if fn == fq else 1 if fn.startswith(fq) else 2 if f" {words[0]}" in f" {fn}" else 3
                scored.append((rank, len(fn), cid))
        self.last_fuzzy = not scored     # 调用方 (pick) 要知道这是猜的, 猜的不能直接拿去改卡
        if not scored:
            scored = self._fuzzy(words)
        scored = [t for t in scored if self._visible(t[2])]
        scored.sort()
        return [(cid, self.names[cid]) for _, _, cid in scored[:limit]]

    def _fuzzy(self, words):
        """拼错兜底: "mitsurigi" -> Mitsurugi. 每个词换成词表里最像的几个, 再按包含查. 只在精确查不到时走"""
        import difflib
        if not hasattr(self, "_vocab"):
            self._vocab = sorted({w for _, fn in self._folded for w in fn.split() if len(w) > 2})
        alts = []
        for w in words:
            near = difflib.get_close_matches(w, self._vocab, n=6, cutoff=0.75) if len(w) > 3 else []
            alts.append([w] + near)
        out = []
        for cid, fn in self._folded:
            toks = fn.split()
            if all(any(a in fn if a == w else a in toks for a in opts) for w, opts in zip(words, alts)):
                out.append((4, len(fn), cid))     # 排在所有精确结果后面
        return out
