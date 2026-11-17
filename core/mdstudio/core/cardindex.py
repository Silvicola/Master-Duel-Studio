"""卡号到卡片信息的动态索引。

数据源全部是游戏当前文件，不写死任何卡号、号段、数量或文件清单：
  当前账号 LocalData 下的 CARD_* / CardPictureFontSetting
  安装目录 data.unity3d 里的 card_rarity_asset / cards_all / cards_in_maindeck / of_card_asset
打开软件时比一次指纹，变了就重建，缓存落在程序目录的 data 下，按账号分开。

语义（subtype 叫什么、用哪张卡框）放在随包 tools/cards/subtype_semantics.json，
这里只保留 CARD_Prop 读出的原始码。游戏新增卡号只要进了任意一张表，并集自动变大。
"""
import json
import logging
import os
import re
import struct
import threading
import time
from collections import defaultdict
from pathlib import Path

from mdstudio import runtime
from mdstudio.core.crc import get_crc
from mdstudio.core.journal import home_dir
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")

SCHEMA_VERSION = 1
INDEX_FILE = "cards_index.json"
WARN_FILE = "carddata_warnings.log"
SEMANTICS_FILE = "tools/cards/subtype_semantics.json"

# 当前账号下会去读的文本表。缺哪个就记进未解析来源，不影响其它表。
LOCAL_FILES = ("CARD_Name", "CARD_Indx", "CARD_Desc", "CARD_Prop", "CARD_Same",
               "CARD_Genre", "CARD_Link", "CARD_RubyName", "CARD_RubyIndx",
               "CARD_IntID", "Card_Pidx", "Card_Part")
# data.unity3d 里能给出卡号的表
UNITY_TABLES = ("card_rarity_asset", "cards_all", "cards_in_maindeck", "of_card_asset")

# 名字恰好叫「未知」的空号（当前是 3001、21219）。只排完全相等的，
# 「未知青蛙」「未知同步士」这类真卡不受影响。
_HIDDEN_NAMES = {"未知", "unknown", "Unknown"}
_TOKEN_SUBTYPES = (10, 49)
_PLAIN_SUBTYPES = (0, 10, 49)

_MEM = {}
_LOCK = threading.Lock()
_SEMANTICS = None
_WARN_KEYS = None
_WARN_PATH = None


def semantics() -> dict:
    """随包语义表，读一次缓存。它只提供名称与卡框编号，不参与卡号判定。"""
    global _SEMANTICS
    if _SEMANTICS is None:
        p = runtime.bundle_root() / SEMANTICS_FILE
        try:
            _SEMANTICS = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning(tr("card type semantics unavailable: %s"), p)
            _SEMANTICS = {"subtypes": {}, "frame_png_map": {}, "coarse_names": {}}
    return _SEMANTICS


def frame_png_for(code: int):
    """给 overframe 用：b2 原值 -> 卡框 PNG 编号。表外返回 None。"""
    if code is None or code < 0:
        return None
    e = semantics()["subtypes"].get(str(code & 0x3F))
    return e.get("frame_png") if e else None


def warn(game, kind: str, detail: str, key: str) -> None:
    """维护用告警日志，按 key 去重，避免每次启动刷屏。"""
    global _WARN_KEYS, _WARN_PATH
    full = f"{key}|{kind}"
    if _WARN_KEYS is not None and full in _WARN_KEYS:
        return
    # 去重必须在算路径之前：这张表每遇到一个未识别卡型就调一次 warn，
    # 冷启动时 14000 多次。home_dir() 每次都 stat 一遍程序目录（含一次 legacy
    # 迁移探测），实测这一项白烧 0.75 秒，全花在重复解析同一个路径上。
    if _WARN_PATH is None:
        _WARN_PATH = home_dir() / WARN_FILE
        _WARN_KEYS = set()
        try:
            for line in _WARN_PATH.read_text(encoding="utf-8").splitlines():
                m = re.search(r"key=([^\s]+)", line)
                if m:
                    _WARN_KEYS.add(m.group(1))
        except OSError:
            pass
        if full in _WARN_KEYS:
            return
    p = _WARN_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} [{kind}] account={getattr(game, 'group', '?')} "
                    f"install={getattr(game, 'install_id', '?')} key={key} {detail}\n")
        _WARN_KEYS.add(full)
    except OSError as e:
        log.warning(tr("can't write card data warning log: %s"), e)
    log.warning("[cardindex] %s %s", kind, detail)


def clear() -> None:
    """换账号 / 换游戏目录 / 手动重新同步时，内存索引一并作废。"""
    with _LOCK:
        _MEM.clear()


class CardIndex:
    def __init__(self, game, lang=None):
        self.game = game
        self.lang = lang
        self.folder = None
        self.cards = {}            # {卡号: 全字段}
        self.visible = {}          # {卡号: 卡名}，371 个无数据号与三个特殊号不在这里
        self.by_name = defaultdict(list)
        self.folded = []
        self.fingerprint = {}
        self.warnings = []
        self.unparsed_sources = []
        self.ok = False
        self.error = ""
        self._names = None         # 账号目录下的文件名集合，_all_names() 一次收齐
        self.built_at = ""
        self.unknown_subtypes = set()

    @classmethod
    def ensure(cls, game, lang=None, force=False) -> "CardIndex":
        key = (getattr(game, "install_id", "?"), getattr(game, "group", "?"), lang or "auto")
        with _LOCK:
            hit = _MEM.get(key)
            if hit is not None and not force:
                return hit
        idx = cls(game, lang)
        idx._load_or_build(force=force)
        with _LOCK:
            _MEM[key] = idx
        return idx

    # ---------------------------------------------------------------- 定位与指纹
    def _locate(self):
        """复用 CardDB 的语言探测，拿到 16hex 目录、语言与卡名表。"""
        from mdstudio.core.cards import CardDB
        probe = CardDB(self.game, self.lang).load()
        return probe._folder, probe.lang, probe.names

    def _find(self, logical):
        return self.game.find_logical(logical)

    def _all_names(self) -> set:
        """账号目录下所有文件名（一次 listdir 收齐，供批量存在性判断）。

        逐张卡各调一次 find_logical 时，每次都要算 crc + 拼路径 + stat，
        14671 张实测 337 ms。而整个账号只有 257 个目录、4 万个文件，
        一次收齐只要 260 ms，之后 14671 次集合查询合计 15 ms。
        顺便躲开「每张卡都 stat 一次，杀软逐个扫」的问题。

        用 os.scandir 而不是 iterdir：iterdir 会为 4 万个文件各建一个 Path 对象，
        scandir 只拿名字，实测能省掉一半时间（0.5 秒 → 0.26 秒）。
        """
        if getattr(self, "_names", None) is None:
            names = set()
            try:
                with os.scandir(self.game.root) as dirs:
                    for d in dirs:
                        if not d.is_dir():
                            continue
                        try:
                            with os.scandir(d.path) as files:
                                for f in files:
                                    names.add(f.name)
                        except OSError:
                            continue
            except OSError:
                pass
            self._names = names
        return self._names

    def _stat(self, p):
        return [p.name, p.stat().st_size, int(p.stat().st_mtime)]

    def _fingerprint(self, folder, lang) -> dict:
        local = {}
        for n in LOCAL_FILES:
            p = self._find(f"Card/Data/{folder}/{lang}/{n}")
            if p:
                local[n] = self._stat(p)
        p = self._find(f"Card/ScriptableObjects/{lang}/CardPictureFontSetting")
        if p:
            local["CardPictureFontSetting"] = self._stat(p)
        u = self.game.dir / "masterduel_Data" / "data.unity3d"
        sp = runtime.bundle_root() / SEMANTICS_FILE
        return {"account": getattr(self.game, "group", "?"), "folder": folder,
                "lang": lang, "local": local,
                "unity3d": self._stat(u) if u.is_file() else [],
                "semantics": self._stat(sp) if sp.is_file() else []}

    def _cache_path(self) -> Path:
        return home_dir() / self.game.install_id / self.game.group / INDEX_FILE

    # ---------------------------------------------------------------- 读写缓存
    def _load_or_build(self, force: bool = False):
        from mdstudio import progress

        progress.step(tr("locating game data"))
        try:
            folder, lang, names = self._locate()
        except Exception as e:
            self.error = str(e)
            warn(self.game, "parse_error", f"locate failed: {e}", "locate")
            return
        self.folder, self.lang = folder, lang
        fp = self._fingerprint(folder, lang)
        path = self._cache_path()
        cached = None
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        # force 用来实现「手动重新同步」：跳过磁盘缓存，真的重读一遍游戏数据。
        # 只清内存实例是不够的，那只是让 ensure() 不再命中 _MEM，下面照样会 adopt 缓存。
        fresh = (not force and cached
                 and cached.get("schema_version") == SCHEMA_VERSION
                 and cached.get("fingerprint") == fp)
        if fresh:
            self._adopt(cached)
            return
        try:
            data = self._build(folder, lang, names, fp)
        except Exception as e:
            self.error = str(e)
            warn(self.game, "parse_error", f"build failed: {e}",
                 f"build|{fp.get('unity3d')}")
            if cached:
                self._adopt(cached)
                self.ok = False
                self.error = str(e)
                warn(self.game, "stale_cache",
                     "using previous index for read only, writes are blocked",
                     f"stale|{fp.get('unity3d')}")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            log.warning(tr("can't save the card index cache: %s"), e)
        self._adopt(data)

    # ---------------------------------------------------------------- 构建
    def _read_local(self, folder, lang):
        out = {}
        for n in LOCAL_FILES:
            p = self._find(f"Card/Data/{folder}/{lang}/{n}")
            if not p:
                continue
            try:
                from mdstudio.core.cards import _textasset, text_decrypt, find_key
                enc = _textasset(p.read_bytes())
                out[n] = text_decrypt(enc, find_key(enc))
            except Exception:
                self.unparsed_sources.append(n)
        for n in LOCAL_FILES:
            if n not in out and n not in self.unparsed_sources:
                self.unparsed_sources.append(n)
        return out

    def _read_unity(self):
        """data.unity3d 里那四张卡表。

        解一遍 128MB 要 1.5 秒，而真正要的东西只有 107KB。指纹没变就复用
        上次抽出来的那四张表，别再解一遍。缓存跟着账号索引放同一个目录。
        """
        p = self.game.dir / "masterduel_Data" / "data.unity3d"
        if not p.is_file():
            return {}
        stamp = self._stat(p)
        cache = self._cache_path().with_name("unity_tables.json")
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if raw and raw.get("stat") == stamp:
            try:
                return {k: bytes.fromhex(v) for k, v in raw["tables"].items()}
            except (KeyError, TypeError, ValueError):
                pass

        out = {}
        import UnityPy      # 惰性导入：命中 unity 表缓存时用不到，导入一次要 0.9 秒
        for o in UnityPy.load(p.read_bytes()).objects:
            if o.type.name != "TextAsset":
                continue
            try:
                d = o.read()
                n = str(d.m_Name)
                if n not in UNITY_TABLES:
                    continue
                s = d.m_Script
                out[n] = s.encode("utf-8", "surrogateescape") if isinstance(s, str) else bytes(s)
            except Exception:
                continue
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"stat": stamp,
                                       "tables": {k: v.hex() for k, v in out.items()}}),
                           encoding="utf-8")
            os.replace(tmp, cache)
        except OSError as e:
            log.warning(tr("can't save the unity table cache: %s"), e)
        return out

    def _build(self, folder, lang, names, fp):
        from mdstudio import progress

        progress.step(tr("reading card tables"))
        L = self._read_local(folder, lang)
        progress.step(tr("reading game data tables"))
        U = self._read_unity()

        def u16(b, i):
            return struct.unpack_from("<H", b, i)[0]

        font_ids, fontsize = [], {}
        p = self._find(f"Card/ScriptableObjects/{lang}/CardPictureFontSetting")
        import UnityPy
        for o in UnityPy.load(p.read_bytes()).objects:
            if o.type.name != "MonoBehaviour":
                continue
            tt = o.read_typetree()
            if "m_CardidNormalFontSizePairDatas" in tt:
                for e in tt["m_CardidNormalFontSizePairDatas"]:
                    c = e["cardid"]
                    font_ids.append(c)
                    fontsize[c] = e.get("fontsize")
                break
        if not font_ids:
            raise RuntimeError("CardPictureFontSetting has no card id list")

        indx = L.get("CARD_Indx", b"")
        noff = [struct.unpack_from("<I", indx, i)[0]
                for i in range(0, len(indx) - 7, 8)]
        doff = [struct.unpack_from("<I", indx, i + 4)[0]
                for i in range(0, len(indx) - 7, 8)]

        def cut(blob, offs):
            return [blob[a:b].decode("utf-8", "replace").rstrip("\x00")
                    for a, b in zip(offs[1:], offs[2:])]

        name_list = cut(L.get("CARD_Name", b""), noff) if noff else []
        desc_list = cut(L.get("CARD_Desc", b""), doff) if doff else []
        ruby_list = []
        if "CARD_RubyName" in L and "CARD_RubyIndx" in L:
            ri = L["CARD_RubyIndx"]
            roff = [struct.unpack_from("<I", ri, i)[0] for i in range(0, len(ri) - 7, 8)]
            if roff:
                ruby_list = cut(L["CARD_RubyName"], roff)
        id2name = dict(zip(font_ids, name_list))
        id2desc = dict(zip(font_ids, desc_list))
        id2ruby = dict(zip(font_ids, ruby_list))
        # CardDB 已经解过一次卡名，优先用它的结果，避免两套切分不一致
        for c, nm in names.items():
            id2name[c] = nm

        prop, b2 = {}, {}
        raw = L.get("CARD_Prop", b"")
        for i in range(8, len(raw) - 7, 8):
            v = int.from_bytes(raw[i:i + 8], "little")
            c = v & 0xFFFF
            if c:
                prop[c] = v
                b2[c] = raw[i + 2]

        genre = {}
        if "CARD_Genre" in L:
            g = L["CARD_Genre"]
            for i, c in enumerate(font_ids):
                if 8 + i * 8 + 8 <= len(g):
                    genre[c] = hex(int.from_bytes(g[8 + i * 8:8 + i * 8 + 8], "little"))

        aliases = defaultdict(list)
        sm = L.get("CARD_Same", b"")
        for i in range(0, len(sm) - 5, 6):
            a, b, flag = struct.unpack_from("<HHH", sm, i)
            if a and b:
                aliases[a].append([b, flag])
                aliases[b].append([a, flag])
        links = defaultdict(list)
        lk = L.get("CARD_Link", b"")
        for i in range(0, len(lk) - 3, 4):
            a, b = struct.unpack_from("<HH", lk, i)
            if a:
                links[a].append(b)

        rarity = {}
        rr = U.get("card_rarity_asset", b"")
        for i in range(0, len(rr) - 3, 4):
            c, r = struct.unpack_from("<HH", rr, i)
            if c:
                rarity[c] = r
        cards_all = {u16(U.get("cards_all", b""), i)
                     for i in range(0, len(U.get("cards_all", b"")) - 1, 2)}
        maindeck = {u16(U.get("cards_in_maindeck", b""), i)
                    for i in range(0, len(U.get("cards_in_maindeck", b"")) - 1, 2)}
        gate = []
        gb = U.get("of_card_asset", b"")
        for i in range(0, len(gb) - 3, 4):
            gate.append(struct.unpack_from("<HH", gb, i))
        base_of = {t: b for t, b in gate if t and b and t != b}

        union = set()
        union |= set(prop) | set(font_ids) | set(rarity) | cards_all | maindeck
        for t, b in gate:
            if t:
                union.add(t)
            if b:
                union.add(b)
        union |= set(aliases) | set(links)
        union.discard(0)

        sem = semantics()
        sub_table = sem.get("subtypes", {})
        token_ids = {c for c, v in prop.items() if ((v >> 16) & 0x3F) in _TOKEN_SUBTYPES}
        trigger_set = {t for t, _ in gate if t}
        base_set = set(base_of.values())
        by_name = defaultdict(list)
        for c in font_ids:
            if id2name.get(c):
                by_name[id2name[c]].append(c)

        cards = {}
        unknown = set()
        present = self._all_names()
        find_crc = get_crc
        ids = sorted(union)
        total = len(ids)
        for n, cid in enumerate(ids):
            # 这一段占了整个构建的一半以上，界面靠它把进度条从 0 推到 100。
            # 每 200 张报一次就够了，报太密会让 stdout 变成瓶颈。
            if n % 200 == 0:
                progress.tick(tr("parsing cards"), n, total)
            v = prop.get(cid)
            name = id2name.get(cid, "")
            has_art = find_crc(f"Card/Images/Illust/tcg/{cid}") in present
            in_tables = []
            if cid in prop:
                in_tables.append("CARD_Prop")
            if cid in font_ids:
                in_tables.append("FontSetting")
            if cid in rarity:
                in_tables.append("rarity")
            if cid in cards_all:
                in_tables.append("cards_all")
            if cid in maindeck:
                in_tables.append("maindeck")
            if cid in trigger_set:
                in_tables.append("overframe_trigger")
            if cid in base_set:
                in_tables.append("overframe_base")
            if cid in aliases:
                in_tables.append("CARD_Same")
            if cid in links:
                in_tables.append("CARD_Link")

            sub = ((v >> 16) & 0x3F) if v is not None else None
            inferred = None
            if sub is None and cid in base_of:
                vb = prop.get(base_of[cid])
                if vb is not None:
                    inferred = (vb >> 16) & 0x3F
            effective = sub if sub is not None else inferred
            ent = sub_table.get(str(effective)) if effective is not None else None
            if effective is not None and ent is None:
                unknown.add(effective)
                warn(self.game, "unknown_subtype",
                     f"subtype={effective} card={cid} name={name!r}",
                     f"subtype|{effective}")

            # 临时特殊卡名：游戏把效果文本里替换出来的假卡名也归进 Token 类。
            # 它们没有衍生物字样、不在稀有度表、也没有卡图，跟真衍生物区分得开。
            token_named = ("衍生物" in name or "Token" in name or "token" in name)
            hidden = (not name) or name in _HIDDEN_NAMES or (
                effective in _TOKEN_SUBTYPES and not token_named
                and cid not in rarity and not has_art)
            placeholder = 0
            if name and cid not in rarity:
                for sib in by_name.get(name, ()):
                    if sib != cid and sib in rarity:
                        placeholder = sib
                        break
            unreleased = bool(name) and name not in _HIDDEN_NAMES and not hidden \
                and cid not in rarity and not placeholder and cid not in token_ids
            kind = ""
            if effective is not None:
                kind = "spell" if effective == 13 else "trap" if effective == 14 else "monster"
            plain = effective in _PLAIN_SUBTYPES if effective is not None else False

            row = {
                "id": cid, "name": name, "desc": id2desc.get(cid, ""),
                "ruby": id2ruby.get(cid, ""),
                "subtype": sub, "b2": b2.get(cid), "prop": v,
                "subtype_inferred": inferred,
                "attr": ((v >> 22) & 0xF) if v is not None else None,
                "lvl": ((v >> 26) & 0xF) if v is not None else None,
                "atk": ((v >> 32) & 0x1FF) if v is not None else None,
                "def": ((v >> 41) & 0x1FF) if v is not None else None,
                "arrows": ((v >> 41) & 0xFF) if v is not None else None,
                "race": ((v >> 53) & 0x1F) if v is not None else None,
                "stype": ((v >> 50) & 0x7) if v is not None else None,
                "scale": ((v >> 59) & 0x1F) if v is not None else None,
                "genre": genre.get(cid), "rarity": rarity.get(cid),
                "fontsize": fontsize.get(cid),
                "in": in_tables, "base": base_of.get(cid),
                "aliases": aliases.get(cid, []), "links": links.get(cid, []),
                "art": has_art,
                "frame_png": ent.get("frame_png") if ent else None,
                "frame_type": ent.get("frame_type") if ent else None,
                "coarse": ent.get("coarse") if ent else None,
                "coarse_cn": ent.get("coarse_cn") if ent else None,
                "subtype_cn": ent.get("cn") if ent else None,
                "abilities": ent.get("abilities", []) if ent else [],
                "unknown_subtype": effective is not None and ent is None,
                "frame_inferred": sub is None and inferred is not None,
                "kind": kind, "plain": plain, "unreleased": unreleased,
                "hidden": hidden, "visible": not hidden,
                "placeholder": placeholder,
                "semantic_undetermined": ["genre", "links", "aliases_flag", "ruby"],
            }
            cards[str(cid)] = row

        self.unknown_subtypes = unknown
        if unknown:
            warn(self.game, "unknown_subtype_summary",
                 f"values={sorted(unknown)} count={sum(1 for r in cards.values() if r['unknown_subtype'])}",
                 "subtype_summary|" + ",".join(map(str, sorted(unknown))))
        return {"schema_version": SCHEMA_VERSION, "fingerprint": fp,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "lang": lang, "folder": folder, "ok": True,
                "unparsed_sources": sorted(set(self.unparsed_sources)),
                "unknown_subtypes": sorted(unknown), "cards": cards}

    # ---------------------------------------------------------------- 采用
    def _adopt(self, data):
        self.cards = {int(k): v for k, v in data.get("cards", {}).items()}
        self.fingerprint = data.get("fingerprint", {})
        self.folder = data.get("folder")
        self.lang = data.get("lang", self.lang)
        self.built_at = data.get("built_at", "")
        self.unparsed_sources = data.get("unparsed_sources", [])
        self.unknown_subtypes = set(data.get("unknown_subtypes", []))
        self.ok = bool(data.get("ok", True)) and not self.error
        self.visible = {}
        self.by_name = defaultdict(list)
        for cid, row in self.cards.items():
            nm = row.get("name", "")
            if row.get("visible") and nm:
                self.visible[cid] = nm
                self.by_name[nm].append(cid)
        # folded 只在第一次文字搜索时才算，命中缓存的启动不该付这 100 ms
        self.folded = None
        log.info(tr("card index: %d ids, %d visible (%s)"),
                 len(self.cards), len(self.visible), self.lang)

    # ---------------------------------------------------------------- 查询
    def row(self, cid):
        return self.cards.get(int(cid))

    def name(self, cid):
        r = self.row(cid)
        return (r or {}).get("name", "")

    def frame_code(self, cid):
        r = self.row(cid)
        if not r:
            return -1
        b = r.get("b2")
        if b is None and r.get("subtype_inferred") is not None:
            return r["subtype_inferred"]
        return b if b is not None else -1

    def frame_png(self, cid):
        r = self.row(cid)
        return (r or {}).get("frame_png")

    def kind(self, cid):
        r = self.row(cid)
        return (r or {}).get("kind", "")

    def is_plain(self, cid):
        r = self.row(cid)
        return bool(r and r.get("plain"))

    def is_unreleased(self, cid):
        r = self.row(cid)
        return bool(r and r.get("unreleased"))

    def is_unknown_subtype(self, cid):
        r = self.row(cid)
        return bool(r and r.get("unknown_subtype"))

    def placeholder_of(self, cid):
        r = self.row(cid)
        return (r or {}).get("placeholder", 0)

    def same_name(self, cid):
        r = self.row(cid)
        nm = (r or {}).get("name", "")
        return sorted(self.by_name.get(nm, [int(cid)])) if nm else [int(cid)]

    def visible_names(self):
        return dict(self.visible)

    def status(self, cid):
        r = self.row(cid) or {}
        return {"known": bool(r), "unreleased": r.get("unreleased", False),
                "unknown_subtype": r.get("unknown_subtype", False),
                "subtype": r.get("subtype"), "subtype_cn": r.get("subtype_cn"),
                "coarse_cn": r.get("coarse_cn"), "abilities": r.get("abilities", []),
                "hidden": r.get("hidden", True), "locked": bool(r) and (
                    r.get("unreleased") or r.get("unknown_subtype")),
                "writable": self.ok and bool(r) and not (
                    r.get("unreleased") or r.get("unknown_subtype"))}

    def _ensure_folded(self):
        if self.folded is None:
            from mdstudio.core.cards import fold
            self.folded = [(cid, fold(nm)) for cid, nm in self.visible.items()]
        return self.folded

    def search(self, query, limit=50):
        q = (query or "").strip()
        if not q:
            return []
        if q.isascii() and q.isdigit():
            hits = [c for c in self.visible if str(c).startswith(q)]
            hits.sort(key=lambda c: (str(c) != q, c))
            return [(c, self.visible[c]) for c in hits[:limit]]
        from mdstudio.core.cards import fold
        fq = fold(q)
        words = fq.split()
        scored = []
        for cid, fn in self._ensure_folded():
            if all(w in fn for w in words):
                rank = 0 if fn == fq else 1 if fn.startswith(fq) else 2 if f" {words[0]}" in f" {fn}" else 3
                scored.append((rank, len(fn), cid))
        scored.sort()
        return [(cid, self.visible[cid]) for _, _, cid in scored[:limit]]
