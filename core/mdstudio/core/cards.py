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

from mdstudio.core.journal import home_dir
from mdstudio.errors import AssetMissing
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")

_CONST = 0x23D          # 公式里的固定常数, 不是 key
_KEY_LIMIT = 0x400      # key 每次大更新会变, 历史最大 0x25F
_SIZE_HINT = 116419     # CARD_Name bundle 大概这么大, 扫的时候按接近程度排序
# 游戏支持的语言，探测 16hex 目录时逐个试。只影响首次定位，命中就记进缓存。
_LANGS = ("en-US", "ja-JP", "zh-CN", "zh-TW", "ko-KR", "de-DE", "fr-FR", "it-IT",
          "es-ES", "es-MX", "pt-BR", "ru-RU")
_CONTAINER_RE = re.compile(rb"card/data/([0-9a-f]{16})/([a-z]{2}-[a-z]{2,4})/card_name")
# data.unity3d 里只留了目录，没有 card_name 这一级（文件名是 crc），所以这里放宽
_UNITY_DIR_RE = re.compile(rb"card/data/([0-9a-f]{16})/([a-z]{2}-[a-z]{2,4})")


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
    import UnityPy      # 惰性导入：命中索引缓存时用不到它，导入一次要 0.9 秒
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


# 卡号、卡型、稀有度、超框关系的查询统一走 core/cardindex.py 的动态索引；
# 这里只留卡名表与解密工具，供索引定位语言与 16hex 目录时复用。


class CardDB:
    def __init__(self, game, lang: str = None):
        """lang=None: 用游戏当前的语言 (盘上只有那一份)"""
        self.game = game
        self.lang = lang
        self._auto = lang is None
        self.names = {}           # id -> name
        self._folded = []         # [(id, folded name)]
        self._folder = None       # CARD_Name 所在的那个 16hex 目录, load() 里定下来
        self._index = None        # 动态卡片索引, 第一次查询时才挂上
        self._cache = home_dir() / game.install_id / f"cards_{lang or 'auto'}.json"

    def _idx(self):
        """全字段索引。索引构建失败也返回对象，调用方按 ok 决定是否放行写入。"""
        if self._index is None:
            try:
                from mdstudio.core.cardindex import CardIndex
                self._index = CardIndex.ensure(self.game, self.lang)
            except Exception as e:
                log.warning(tr("card index unavailable: %s"), e)
                from mdstudio.core.cardindex import CardIndex
                self._index = CardIndex(self.game, self.lang)
        return self._index

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
        return self._probe_from_unity3d() or self._probe_by_bundles()

    def _probe_from_unity3d(self):
        """data.unity3d 里就写着卡名数据在哪个 16hex 目录，不必翻 4 万个 bundle。

        实测这条路 90 ms，逐个解包的旧路要 6.5 秒（用户重装游戏、文件还冷时更久）。
        游戏只把当前语言那一份下到 LocalData，所以目录名能直接读到、语言得逐个试：
        每个候选只花一次 stat（crc 命中即中），试不中也才十几毫秒。
        """
        p = self.game.dir / "masterduel_Data" / "data.unity3d"
        try:
            raw = p.read_bytes()
        except OSError:
            return None
        dirs = sorted({m for m in _UNITY_DIR_RE.findall(raw)})
        for folder, _ in dirs:
            folder = folder.decode()
            for cand in _LANGS:
                if self._accept(folder, cand.lower()):
                    return folder
        return None

    def _probe_by_bundles(self):
        """兜底：data.unity3d 里找不到时，按大小逐个解 bundle 翻容器名。慢但可靠。"""
        import UnityPy
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
            self._folder = folder
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
        import UnityPy
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
        self._folder = folder
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
        """占位副本号返回游戏真正渲染的那个号, 其余(含异画)返回 0。"""
        return self._idx().placeholder_of(cid)

    def _visible(self, cid: int) -> bool:
        """可见性由动态索引给：无卡名无卡图的预留号、空号、临时特殊卡名都隐藏。"""
        row = self._idx().row(cid)
        return bool(row.get("visible")) if row else False

    def visible_names(self) -> dict:
        """给「列出全部卡」用，跟 search() 同一套可见性规则。"""
        return self._idx().visible_names()

    # ---- 卡种（怪兽 / 魔法 / 陷阱） ----

    def type_code(self, cid: int) -> int:
        """CARD_Prop 第 3 字节原值，从动态索引取。

        索引按卡号并集构建，超框变体查不到自己数据时跟着 of_card_asset 的底图走，
        并且把这类值标成 inferred。卡种、卡框、可见性统一走同一份索引。
        """
        return self._idx().frame_code(cid)

    def frame_code(self, cid: int) -> int:
        """CARD_Prop 第 3 字节原值，卡图页用它挑卡框。查不到返回 -1。

        卡框不能只看卡种（怪兽/魔法/陷阱）：融合、同调、超量、灵摆各是另一套框。
        这个字节的低 6 位就是游戏自己的卡种枚举，高 2 位是标志位。
        """
        return self.type_code(cid)

    def kind(self, cid: int) -> str:
        """卡种：`monster` / `spell` / `trap`；游戏数据里确实没有这个号时返回空串。"""
        return self._idx().kind(cid)

    def is_plain(self, cid: int) -> bool:
        """通常怪兽 / 衍生物：游戏不会给这类卡做发动特效，特效搜索里该剔掉。"""
        return self._idx().is_plain(cid)

    def is_unreleased(self, cid: int) -> bool:
        """新卡还没上线：有卡名但不在稀有度表，且不是占位、衍生物、隐藏卡或空号。

        判据在动态索引里，按「这个号出现在哪些表」算，不写号段。游戏更新后
        稀有度表一变，索引重建，这个判断自动跟着变。
        """
        return self._idx().is_unreleased(cid)

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
