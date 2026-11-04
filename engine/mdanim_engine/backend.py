"""给 UI 用的数据整形.

复用 mdstudio 包, 这里只做两件事: 缓存重的查询, 把字段裁成 UI 要的形状.
"""
import threading
from pathlib import Path

from mdstudio import runtime, settings

# 卡片状态原值是 game / yours / wiped / None, 界面上按标签分三档.
# 筛选参数用的就是这三档, 前后端同一套词, 免得两边各写一份映射.
_KIND = {"game": "official", "yours": "self", "wiped": "self"}


def _kind(state) -> str:
    return _KIND.get(state or "", "none")


_ART_ORDER = ("cutin", "runfx", "art")


def _kind_order(kinds) -> list:
    """卡的类型按 动画 → 特效 → 卡图 排, 去重."""
    want = [k for k in _ART_ORDER if k in kinds]
    return want + [k for k in sorted(set(kinds)) if k and k not in want]


def _pass(row: dict, cutin: str, runfx: str) -> bool:
    """同时满足两个维度的筛选条件才留下. 空串表示这一维不限。"""
    if cutin and _kind(row.get("cutin")) != cutin:
        return False
    if runfx and _kind(row.get("runfx")) != runfx:
        return False
    return True


class Backend:
    def __init__(self):
        self._db = None
        self._status = None          # (cards, official)
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """动作跑完之后调: 卡的状态变了, 缓存必须作废"""
        with self._lock:
            self._status = None

    def reset(self) -> None:
        """换了游戏目录或账号: 连卡名表一起作废, 否则会拿着旧账号的数据"""
        with self._lock:
            self._db = None
            self._status = None

    # ---------------------------------------------------------------- 上下文
    def _ctx(self):
        from mdstudio.core.context import Ctx
        return Ctx.open(None, dry_run=False)

    def _names(self, ctx):
        if self._db is None:
            from mdstudio.core.cards import CardDB
            self._db = CardDB(ctx.game).load()
        return self._db

    def _rows(self, ctx):
        with self._lock:
            if self._status is None:
                from mdstudio.ops import cutin as C
                from mdstudio.ops import runfx as R

                cut = dict(C.list_cards(ctx.game, ctx.journal))
                fx = dict(R.list_cards(ctx.game, ctx.journal))
                # 两边合并: 只列有召唤动画的卡, 发动特效那 60 多张卡就选不到了
                ids = sorted(set(cut) | set(fx))
                cards = [{"id": c, "cutin": cut.get(c), "runfx": fx.get(c)} for c in ids]
                official = [c for c, st in cut.items() if st == "game"]
                self._status = (cards, official)
            return self._status

    # ------------------------------------------------------------------ 查询
    def info(self) -> dict:
        from mdstudio import __build__, __version__

        ctx = self._ctx()
        g = ctx.game
        return {
            "version": __version__,
            "build": __build__,
            "game_dir": str(g.dir),
            "account": g.group,
            "account_label": settings.label(g.group),
            "accounts": g.accounts(),
            "journal": str(ctx.journal.folder),
            "records": len(ctx.journal.load()),
        }

    def status(self) -> dict:
        ctx = self._ctx()
        db = self._names(ctx)
        cards, official = self._rows(ctx)
        off = set(official)
        for c in cards:
            c["name"] = db.name(c["id"]) or ""
            c["official"] = c["id"] in off
        return {"cards": cards, "count": len(cards)}

    def search(self, q: str = "", limit: int = 200,
               cutin: str = "", runfx: str = "",
               has_anim: bool = True) -> dict:
        """搜卡. 空串 = 有召唤动画或发动特效的卡全列

        有查询词时从**完整卡名表**里搜(14257 张), 不只是本工具动过的那几张,
        否则"耀圣"这种能匹配很多卡的词只出得来几张. 搜到的卡再把动画状态补上,
        没有动画的卡状态是 None, 界面上显示"无".

        cutin / runfx 是标签筛选, 取值 official / self / none, 空串表示不限.

        has_anim=False 时不补动画状态、空串就列全部卡名表. 卡图页要的是所有卡,
        跟动画页那个"只收有动画或有特效的卡"的列表不是一回事。
        """
        ctx = self._ctx()
        db = self._names(ctx)
        cards, _ = self._rows(ctx)
        q = (q or "").strip()

        if not has_anim:
            if not q:
                rows = [{"id": cid, "name": nm} for cid, nm in
                        sorted(db.visible_names().items(), key=lambda kv: kv[0])]
                total = len(rows)
                rows = rows[:limit]
            else:
                rows = [{"id": cid, "name": nm} for cid, nm in db.search(q, limit=limit)]
                total = len(rows)
            # real != 0 表示这个号是占位副本, 值是游戏真正渲染的号;
            # unreleased=True 表示名字在表里但还没上线(不在稀有度表、非占位、非衍生物)
            for r in rows:
                r["real"] = db.placeholder_of(r["id"])
                r["unreleased"] = db.is_unreleased(r["id"])
            return {"cards": rows, "count": total}

        filt = bool(cutin or runfx)
        if not q:
            out = [{"id": c["id"], "name": db.name(c["id"]) or "",
                    "cutin": c["cutin"], "runfx": c["runfx"],
                    "real": db.placeholder_of(c["id"])} for c in cards]
            out = [r for r in out if _pass(r, cutin, runfx)]
            return {"cards": out, "count": len(out)}

        state = {c["id"]: c for c in cards}
        seen = set()
        out = []
        # 带筛选时把候选放大: 先在 limit 处截断再过滤, 会把真正符合条件的结果漏掉
        want = limit * 10 if filt else limit
        for cid, name in db.search(q, limit=want):
            if cid in seen:
                continue
            seen.add(cid)
            c = state.get(cid)
            out.append({"id": cid, "name": name,
                        "cutin": c["cutin"] if c else None,
                        "runfx": c["runfx"] if c else None,
                        "real": db.placeholder_of(cid)})

        # 卡名表里没匹配上的, 但状态表里有且名字含查询词的, 也要能搜到
        ql = q.lower()
        for c in cards:
            if c["id"] in seen:
                continue
            name = db.name(c["id"]) or ""
            if ql in name.lower() or ql in str(c["id"]):
                seen.add(c["id"])
                out.append({"id": c["id"], "name": name,
                            "cutin": c["cutin"], "runfx": c["runfx"],
                            "real": db.placeholder_of(c["id"])})
        if filt:
            out = [r for r in out if _pass(r, cutin, runfx)][:limit]
        return {"cards": out, "count": len(out)}


    # ------------------------------------------------------------------ 发动特效
    def runfx_donors(self) -> dict:
        """借用来源: 游戏里每个带发动特效的卡

        on_disk 是 effectPath 指向的 bundle 在不在盘上. 不在的话借过来游戏里放不出来,
        界面上要标出来, 别让人挑一个空壳.
        """
        from mdstudio.ops import runfx as R

        ctx = self._ctx()
        db = self._names(ctx)
        # 一张卡常常有多条 entry(主效果 + 它的 Op 版本), 界面上只该出现一次
        seen = {}
        for mrk, timing, se, ep, on_disk in R.list_effects(ctx.game):
            ok = True if on_disk is None else bool(on_disk)
            cur = seen.get(mrk)
            if cur is None:
                seen[mrk] = {"id": mrk, "name": db.name(mrk) or "",
                             "timing": str(timing or ""), "se": se or "", "ready": ok}
            elif ok and not cur["ready"]:
                cur["ready"] = True
        out = sorted(seen.values(), key=lambda e: e["id"])
        return {"effects": out, "count": len(out)}


    # ------------------------------------------------------------------ 整合包分享
    def pack_list(self) -> dict:
        """本工具加过的卡, 一眼看完, 方便挑哪些导出"""
        from mdstudio.ops import pack as P

        ctx = self._ctx()
        db = self._names(ctx)
        cards = {}
        for card, kinds in P.exportable(ctx.journal).items():
            cards[str(card)] = {"kinds": sorted(kinds), "name": db.name(card) or ""}
        return {"cards": cards, "count": len(cards)}

    def pack_inspect(self, pack_path: str) -> dict:
        """看看别人的包里有什么卡、什么类型

        一张卡的动画/特效/卡图是三条独立条目, 界面上要合成一行: 卡号、卡名、类型一起显示,
        否则同一张卡会重复出现三次。
        """
        from mdstudio.ops import pack as P

        meta = P.inspect(pack_path)
        ctx = self._ctx()
        db = self._names(ctx)
        merged: dict = {}
        for c in meta.get("cards", []):
            mrk = c.get("mrk")
            if not isinstance(mrk, int):
                continue
            slot = merged.setdefault(mrk, {"mrk": mrk, "kinds": [], "entries": 0})
            slot["kinds"].append(c.get("kind"))
            slot["entries"] += len(c.get("catalog") or {})
        cards = []
        for mrk in sorted(merged):
            slot = merged[mrk]
            # 包里只有卡号, 名字要自己查, 界面上才看得出是哪张卡
            cards.append({"kind": ",".join(_kind_order(slot["kinds"])),
                          "mrk": mrk, "name": db.name(mrk) or "",
                          "entries": slot["entries"]})
        return {"name": meta.get("name"), "tool": meta.get("tool"), "cards": cards}

    # ------------------------------------------------------------------ 历史
    def history_list(self) -> dict:
        """这个工具做过的所有改动, 新的在前"""
        import time

        ctx = self._ctx()
        db = self._names(ctx)
        recs = []
        for r in ctx.journal.load():
            src = r.meta.get("donor") or r.meta.get("label") or r.meta.get("pack") or ""
            if isinstance(src, int) and src:
                # donor 记的是卡号, 界面上带上名字好看些
                src = f"{src} {db.name(src)}".strip()
            recs.append({
                "id": r.id,
                "op": r.op,
                "card": r.card,
                "name": db.name(r.card) if r.card else "",
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.time)),
                # meta 里的 donor 有时是卡号(数字), 有时是名字(字符串), 统一成字符串
                "from": str(src),
                # 导出这类不改游戏的操作也记一条, 但没有可撤销的东西, 界面要禁用撤销
                "undoable": not r.meta.get("noop"),
                # 整合包条目带卡数, 历史里直接显示"导出 2 张卡"
                "cards": r.meta.get("cards"),
                "total": r.meta.get("total"),
            })
        recs.reverse()
        return {"records": recs, "count": len(recs)}

    # ------------------------------------------------------------------ 音效
    def sound_slots(self) -> dict:
        """游戏里所有发动特效用到的音效槽

        只有盘上有的槽能换声音: 全新 label 游戏不认, 换了也是静音.
        顺带给出每个槽被多少张卡用着, 换之前得让人看见影响面.
        """
        from mdstudio.core.gate import RunFxGate
        from mdstudio.ops import sound as S

        ctx = self._ctx()
        db = self._names(ctx)
        gate = RunFxGate(ctx.game)
        slots = {}
        for e in gate.entries:
            lb = e.get("seLabel")
            if not lb:
                continue
            s = slots.setdefault(lb, {"label": lb, "cards": set()})
            if e.get("mrk"):
                s["cards"].add(int(e["mrk"]))
        out = []
        for lb, s in sorted(slots.items()):
            cards = sorted(s["cards"])
            out.append({
                "label": lb,
                "on_disk": bool(S.on_disk(ctx.game, lb)),
                # 内置于 data.unity3d 的槽游戏里响得好好的, 只是没有独立文件可换,
                # 跟"文件没下载"是两回事, 界面上要说清楚.
                "builtin": lb in S.BUILTIN,
                "name": db.name(cards[0]) if cards else "",
                "cards": cards,
                "count": len(cards),
            })
        return {"slots": out, "count": len(out)}

    # ------------------------------------------------------------------ 方案
    def schemes_list(self) -> dict:
        from mdstudio.gui import schemes as SCH
        return {"schemes": SCH.load()}

    # ------------------------------------------------------------------ 超框卡图
    def overframe_frames(self) -> dict:
        from mdstudio.ops import overframe as OF

        return {"frames": [{"id": n, "name": OF.frame_name(n)} for n in OF.frames()]}

    def overframe_status(self, card: int) -> dict:
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        data = OF.status(ctx, card)
        # real != 0 表示这个号是占位副本, 值是游戏真正渲染的号
        data["real"] = self._names(ctx).placeholder_of(card)
        return data

    def overframe_preview(self, card: int, art=None, subject=None,
                          scale=1.0, dx=0, dy=0, lore_alpha=1.0,
                          effect="", holo_scope="") -> dict:
        """只出图，不碰游戏文件。界面拖动参数时高频调用，所以不进事务、不写 journal。"""
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        if art is None:
            art = str(OF.base_art(ctx, int(card)))
        out = OF.compose(int(card), art=art, subject=subject,
                         scale=scale, dx=dx, dy=dy, lore_alpha=lore_alpha,
                         effect=effect or OF.EFFECT_PRISMATIC)
        return {"path": str(out)}

    def overframe_layout(self, card: int) -> dict:
        """给前端本地绘制预览用：卡框图路径 + 插图窗几何 + 底色卡图路径。

        如果这张卡之前应用过，返回上次用的素材与参数（存在 edits.json 里），
        界面据此自动加载，方便继续调整；否则用游戏当前卡图当底。
        """
        import os
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        nn = OF.frame_of_card(int(card))
        ax, ay, aw, ah = OF.art_box(nn)
        lx, ly, lw, lh = OF.lore_rect(nn)

        edit = OF.load_edit(int(card)) or {}
        art = edit.get("art") or ""
        if art and os.path.isfile(art):
            base = art
            has_edit = True
        else:
            has_edit = False
            try:
                base = str(OF.base_art(ctx, int(card)))
            except Exception:
                base = ""

        # 效果：老记录可能存的是旧键名，统一规范化
        effect = OF.normalize_effect(edit.get("effect"))

        subj = edit.get("subject") or ""

        # 连接怪兽：额外给两张箭头层，界面自己叠（点亮层不透明、未点亮层跟着
        # 效果框透明度一起淡出）。写进游戏的整卡贴图是同一套逻辑合成的。
        link = {"markers": 0, "lit": "", "dim": ""}
        if nn == OF.LINK_FRAME:
            try:
                mk = OF.markers_of_card(int(card))
                if mk:
                    lp, dp = OF.link_arrow_split_png(
                        nn, runtime.data_dir() / "overframe" / "link", effect, mk)
                    link = {"markers": mk, "lit": str(lp), "dim": str(dp)}
            except Exception:
                pass

        # 两套卡框图 + 三张外沿素材都给，界面切效果时不用再跑一趟
        return {
            "frame": nn,
            "link": link,
            "frame_path": str(OF.frame_path(nn, effect)),
            "frame_path_prismatic": str(OF.frame_path(nn, OF.EFFECT_PRISMATIC)),
            "frame_path_solid": str(OF.frame_path(nn, OF.EFFECT_KEEP)),
            "border_rainbow_path": str(runtime.bundle_root() / OF.BORDER_RAINBOW_FILE),
            "border_glory_path": str(runtime.bundle_root() / OF.BORDER_GLORY_FILE),
            "border_official_path": str(runtime.bundle_root() / OF.BORDER_OFFICIAL_FILE),
            "ax": ax, "ay": ay, "aw": aw, "ah": ah,
            "lore_top": OF.lore_top(nn),
            "lore_x": lx, "lore_y": ly, "lore_w": lw, "lore_h": lh,
            "lore_bottom": ly + lh,
            "card_w": OF.CARD_W, "card_h": OF.CARD_H,
            "overflow_scale": OF.OVERFLOW_SCALE,
            "base_art": base,
            "has_edit": has_edit,
            "edit_art_name": edit.get("art_name", "") if has_edit else "",
            "edit_subject": subj if (subj and os.path.isfile(subj)) else "",
            "edit_subject_name": edit.get("subject_name", "") if has_edit else "",
            "edit_scale": float(edit.get("scale", 1.0)) if has_edit else 1.0,
            "edit_dx": int(edit.get("dx", 0)) if has_edit else 0,
            "edit_dy": int(edit.get("dy", 0)) if has_edit else 0,
            "edit_lore_alpha": float(edit.get("lore_alpha", 1.0)) if has_edit else 1.0,
            "edit_effect": effect if has_edit else OF.EFFECT_PRISMATIC,
        }

    def overframe_gate(self) -> dict:
        """登记成超框的卡号：官方（游戏本来就有）+ 自改（本工具加的）。

        给左列表打标签用：一次读一张登记表就能覆盖整张列表，
        不必逐卡去游戏目录里读纹理，否则 200 张卡的搜索结果要发 200 次请求。

        官方与自改分开返回：一张官方超框卡被自改之后两个标签都要留着，
        撤销自改则只去掉「自改」。
        """
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        official = OF.official_registered(ctx)
        mine = OF.ours(ctx)
        return {"cards": sorted(official | mine),
                "official": sorted(official),
                "ours": sorted(mine)}

    def overframe_current(self, card: int) -> dict:
        """把游戏里这张卡现在的图导出来给界面看，用来判是否已经超框"""
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        img, meta = OF.read_illust(ctx, card)
        if img is None:
            return {"exists": False, "error": meta.get("error")}
        out = OF._default_out(card).with_name(f"{int(card)}_game.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return {"exists": True, "path": str(out),
                "width": meta["width"], "height": meta["height"],
                "overframe": meta["overframe"]}

    # ------------------------------------------------------------------ Rush Duel
    def rushduel_info(self, card: int) -> dict:
        """RD 卡片页要显示的东西：卡名、攻防、效果文本，以及能不能画等级。

        等级游戏本地没有数据（CARD_Prop / ScriptableObjects 里都没有），
        所以 level 恒为 None，界面据此把那一格留空。
        """
        from mdstudio.ops import rushduel as RD

        return RD.card_info(self._ctx(), int(card))

    def rushduel_preview(self, card: int, art=None, subject=None,
                         scale=1.0, dx=0, dy=0, effect="", frame_alpha=1.0) -> dict:
        """只出图，不碰游戏文件。"""
        from mdstudio.ops import rushduel as RD

        ctx = self._ctx()
        info = RD.card_info(ctx, int(card))
        if art is None:
            art = self._base_art_cached(ctx, int(card))
        out = RD.compose(int(card), art=art, name=info["name"], atk=info["atk"],
                         dfn=info["def"], level=info["level"], desc=info["desc"],
                         category=info["category"], attribute_en=info["attribute_en"],
                         race_en=info["race_en"], pendulum_scale=info["scale"],
                         subject=subject, scale=scale, dx=dx, dy=dy, effect=effect,
                         frame_alpha=frame_alpha)
        return {"path": str(out), "info": info}

    def _base_art_cached(self, ctx, card: int) -> str:
        """兜底卡图（游戏当前插图）解一次就缓存，拖动主体时不再反复读游戏文件。"""
        cache = getattr(self, "_rd_base_cache", None)
        if cache is None:
            cache = {}
            self._rd_base_cache = cache
        p = cache.get(card)
        if p and Path(p).is_file():
            return p
        from mdstudio.ops import overframe as OF

        p = str(OF.base_art(ctx, int(card)))
        cache[card] = p
        return p

    def rushduel_layers(self, card: int, art=None, subject=None, scale=1.0,
                        effect="", frame_alpha=1.0) -> dict:
        """预览分层：底图 / 主体 / 信息层。主体由前端本地平移，拖动零延迟。"""
        from mdstudio.ops import rushduel as RD

        ctx = self._ctx()
        info = RD.card_info(ctx, int(card))
        if art is None:
            art = self._base_art_cached(ctx, int(card))
        out = RD.layers(int(card), art=art, name=info["name"], atk=info["atk"],
                        dfn=info["def"], level=info["level"], desc=info["desc"],
                        category=info["category"], attribute_en=info["attribute_en"],
                        race_en=info["race_en"], pendulum_scale=info["scale"],
                        subject=subject, scale=scale, effect=effect,
                        frame_alpha=frame_alpha)
        out["name"] = info["name"]
        out["atk"] = info["atk"]
        out["def"] = info["def"]
        return out

    def rushduel_status(self, card: int) -> dict:
        from mdstudio.ops import rushduel as RD

        return RD.status(self._ctx(), int(card))

    def rushduel_edit(self, card: int) -> dict:
        """这张卡上次写入用的素材与参数，给界面还原用。"""
        from mdstudio.ops import rushduel as RD

        return RD.load_edit(int(card))

    # ------------------------------------------------------------------ 全局外观
    def appearance_status(self) -> dict:
        """RD 主题当前是否生效、备份在哪。"""
        from mdstudio.ops import appearance as AP

        return AP.status(self._ctx().game)

    def appearance_plan(self) -> dict:
        """扫描 data.unity3d，报出会被替换的纹理清单（只读）。"""
        from mdstudio.ops import appearance as AP

        return AP.plan(self._ctx().game)

    def appearance_preview(self, art=None) -> dict:
        from mdstudio.ops import appearance as AP

        return {"path": str(AP.preview(art=art))}

    # ------------------------------------------------------------------ 壁纸
    def wallpaper_status(self) -> dict:
        """大厅壁纸 / 商店背景有没有被我们换过。"""
        from mdstudio.ops import wallpaper as WP

        return WP.status(self._ctx())

    def wallpaper_preview(self, which: str) -> dict:
        """导出游戏里当前那张纹理，给界面看。"""
        from mdstudio.ops import wallpaper as WP

        return WP.preview(self._ctx(), which)

    def wallpaper_previews(self, which: str) -> list:
        """导出这一类下的全部纹理（商店背景有 6 个包）。"""
        from mdstudio.ops import wallpaper as WP

        return WP.previews(self._ctx(), which)

    # ------------------------------------------------------------------ 设置
    def settings_get(self) -> dict:
        """游戏目录与账号. 没存过就试着自动找一份, 但不会替你写进设置"""
        from mdstudio.core.game import detect_game_dir, list_accounts

        saved = settings.load()
        gdir = saved.get("game_dir") or ""
        detected = ""
        try:
            detected = str(detect_game_dir(None))
        except Exception:
            detected = ""
        if not gdir:
            gdir = detected

        accounts = list_accounts(gdir) if gdir else []
        acc = saved.get("account") or ""
        if not settings.valid_account(gdir, acc):
            acc = accounts[0] if accounts else ""

        return {
            "game_dir": gdir,
            "detected": detected,
            "account": acc,
            "accounts": [{"id": a, "label": settings.label(a)} for a in accounts],
        }

    def settings_set(self, game_dir=None, account=None, name=None) -> dict:
        if game_dir is not None:
            settings.save(game_dir=game_dir)
        if account is not None:
            settings.save(account=account)
        if name is not None and account:
            settings.set_account_name(account, name)
        self.reset()
        return self.settings_get()


QUERY = {
    "ping": lambda be, **kw: {"pong": True},
    "info": lambda be, **kw: be.info(),
    "cards.status": lambda be, **kw: be.status(),
    "cards.search": lambda be, q="", limit=200, cutin="", runfx="", has_anim=True, **kw:
        be.search(q, limit, cutin, runfx, has_anim),
    "runfx.donors": lambda be, **kw: be.runfx_donors(),
    "settings.get": lambda be, **kw: be.settings_get(),
    "settings.set": lambda be, **kw: be.settings_set(**kw),
    "pack.list": lambda be, **kw: be.pack_list(),
    "pack.inspect": lambda be, pack_path, **kw: be.pack_inspect(pack_path),
    "history.list": lambda be, **kw: be.history_list(),
    "sound.slots": lambda be, **kw: be.sound_slots(),
    "schemes.list": lambda be, **kw: be.schemes_list(),
    "overframe.frames": lambda be, **kw: be.overframe_frames(),
    "overframe.status": lambda be, card, **kw: be.overframe_status(card),
    "overframe.current": lambda be, card, **kw: be.overframe_current(card),
    "overframe.gate": lambda be, **kw: be.overframe_gate(),
    "overframe.preview": lambda be, card, art=None, subject=None,
        scale=1.0, dx=0, dy=0, lore_alpha=1.0, effect="", holo_scope="", **kw:
        be.overframe_preview(card, art=art, subject=subject,
                             scale=scale, dx=dx, dy=dy, lore_alpha=lore_alpha,
                             effect=effect, holo_scope=holo_scope),
    "overframe.layout": lambda be, card, **kw: be.overframe_layout(card),
    "rushduel.info": lambda be, card, **kw: be.rushduel_info(card),
    "rushduel.preview": lambda be, card, art=None, subject=None,
        scale=1.0, dx=0, dy=0, effect="", frame_alpha=1.0, **kw:
        be.rushduel_preview(card, art=art, subject=subject,
                            scale=scale, dx=dx, dy=dy, effect=effect,
                            frame_alpha=frame_alpha),
    "rushduel.layers": lambda be, card, art=None, subject=None,
        scale=1.0, effect="", frame_alpha=1.0, **kw:
        be.rushduel_layers(card, art=art, subject=subject,
                           scale=scale, effect=effect, frame_alpha=frame_alpha),
    "rushduel.status": lambda be, card, **kw: be.rushduel_status(card),
    "rushduel.edit": lambda be, card, **kw: be.rushduel_edit(card),
    "appearance.status": lambda be, **kw: be.appearance_status(),
    "appearance.plan": lambda be, **kw: be.appearance_plan(),
    "appearance.preview": lambda be, art=None, **kw: be.appearance_preview(art=art),
    "wallpaper.status": lambda be, **kw: be.wallpaper_status(),
    "wallpaper.preview": lambda be, which="lobby", **kw: be.wallpaper_preview(which),
    "wallpaper.previews": lambda be, which="shop", **kw: be.wallpaper_previews(which),
}
