"""全屏终端界面. `mdstudio` 不带参数 / 双击 mdstudio.bat 进来

给懒人用的: 不用记命令, 左边搜卡 (名字/id), 右边按当前卡给出能做的事, 点一下就做.
鼠标键盘都行. 跟 GUI 调同一套 ops.

坑 (OF Tool CLI 那边踩过):
  - 列表必须报 cursor 位置 (get_cursor_position), 不然窗口不跟着滚, 选中项走出屏幕
  - 全屏模式下绝对不能 print, 会把画面搞花. 日志全部走 handler 进日志框
  - 自动化测试一定要开 MDSTUDIO_READONLY=1 或者用沙盒 ctx, 以前测 UI 手滑往真游戏注册过一张假卡
"""
import importlib.util
import logging
import os
import threading
import time

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import merge_key_bindings
from prompt_toolkit.layout import (DynamicContainer, Float, FloatContainer, HSplit, Layout, VSplit,
                                   Window)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, Label, TextArea

from mdstudio import __version__, pick
from mdstudio.core.cards import fold
from mdstudio.core.context import Ctx
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")

READONLY = os.environ.get("MDSTUDIO_READONLY", "") not in ("", "0", "false", "False")
LOG_ROWS = 9
DEFAULT_DONOR = 11016
FILTERS = [(None, tr("all")), ("cutin", tr("has cut-in")), ("runfx", tr("has effect")), ("any", tr("has either")),
           ("none", tr("has neither"))]
# ctx="badge": 和 CLI 的 _badge 共用一套译文, 免得两处翻出两个词
BADGE = {"game": (tr("yes", ctx="badge"), "class:b.game"), "yours": (tr("yours"), "class:b.yours"),
         "wiped": (tr("WIPED"), "class:b.wiped")}


def _has(mod):
    return importlib.util.find_spec(mod) is not None


def _w(s):
    """字符串的显示宽度: 中文是全角, 占两列. 按钮窗口按这个算宽度, 不然中文被裁一半"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


class ClickList:
    """鼠标/键盘都能用的列表. 从 of_cli 搬过来的, 滚动那个坑已经填了"""

    def __init__(self, get_items, render_row, on_select=None, on_activate=None, click_activates=False,
                 empty="(nothing here)", on_escape_up=None, selectable=None):
        self.get_items = get_items
        self.render_row = render_row
        self.on_select = on_select
        self.on_activate = on_activate
        self.click_activates = click_activates
        self.empty = empty
        self.on_escape_up = on_escape_up
        self.selectable = selectable
        self.blocked = None            # () -> bool, 弹窗开着时主界面的列表不响应鼠标
        self.idx = 0
        self.app = None
        self.control = FormattedTextControl(self._fragments, focusable=True, key_bindings=self._kb(),
                                            show_cursor=False,
                                            get_cursor_position=lambda: Point(x=0, y=self.idx))
        self.window = Window(self.control, style="class:list", wrap_lines=False, always_hide_cursor=True)

    def items(self):
        return list(self.get_items() or [])

    def current(self):
        it = self.items()
        return it[self.idx] if 0 <= self.idx < len(it) else None

    def reset(self):
        self.idx = 0
        items = self.items()
        if items and not self._ok(items, 0):
            self._move(1)

    def _focused(self):
        try:
            return self.app.layout.has_focus(self.window)
        except Exception:
            return False

    def _fragments(self):
        items = self.items()
        n = len(items)
        self.idx = 0 if n == 0 else max(0, min(self.idx, n - 1))
        if not items:
            return [("class:dim", "  " + self.empty)]
        foc = self._focused()
        out = []
        for i, it in enumerate(items):
            body = self.render_row(it)
            row = [("", body)] if isinstance(body, str) else list(body)
            if i == self.idx and self._ok(items, i):
                st = "class:sel" if foc else "class:sel.blur"
                row = [(st, "❯ ")] + [(st, t) for _s, t in row]
            else:
                row = [("", "  ")] + row
            h = self._mouse(i)
            out += [(s, t, h) for s, t in row]
            out.append(("", "\n"))
        out.pop()
        return out

    def _mouse(self, i):
        def handler(ev):
            if self.blocked and self.blocked():
                return None
            if ev.event_type == MouseEventType.MOUSE_UP:
                items = self.items()
                if not (0 <= i < len(items)) or not self._ok(items, i):
                    return None
                was = self.idx == i
                self.idx = i
                try:
                    self.app.layout.focus(self.window)
                except Exception:
                    pass
                if (self.click_activates or was) and self.on_activate:
                    self.on_activate(self.current())
                elif self.on_select:
                    self.on_select(self.current())
                return None
            if ev.event_type == MouseEventType.SCROLL_DOWN:
                self._move(3)
                return None
            if ev.event_type == MouseEventType.SCROLL_UP:
                self._move(-3)
                return None
            return NotImplemented
        return handler

    def _ok(self, items, i):
        return self.selectable is None or self.selectable(items[i])

    def _move(self, delta):
        items = self.items()
        n = len(items)
        if not n:
            return
        i = max(0, min(n - 1, self.idx + delta))
        step = 1 if delta >= 0 else -1
        j = i
        while 0 <= j < n and not self._ok(items, j):   # 跳过分隔行
            j += step
        if not 0 <= j < n:
            j = i
            while 0 <= j < n and not self._ok(items, j):
                j -= step
        if 0 <= j < n:
            self.idx = j
        if self.on_select:
            self.on_select(self.current())

    def _kb(self):
        kb = KeyBindings()

        @kb.add("up")
        def _(e):
            items = self.items()
            first = next((k for k in range(len(items)) if self._ok(items, k)), 0)
            if self.idx <= first and self.on_escape_up:
                self.on_escape_up()
            else:
                self._move(-1)

        kb.add("down")(lambda e: self._move(1))
        kb.add("pageup")(lambda e: self._move(-10))
        kb.add("pagedown")(lambda e: self._move(10))
        kb.add("home")(lambda e: self._move(-10 ** 6))
        kb.add("end")(lambda e: self._move(10 ** 6))

        @kb.add("enter")
        def _(e):
            if self.on_activate:
                self.on_activate(self.current())
        return kb


class _ToLog(logging.Handler):
    def __init__(self, tui):
        super().__init__()
        self.tui = tui
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        self.tui.say(self.format(record), record.levelno)


class Act:
    """右边一行: label, 做什么, 是否写游戏文件"""

    def __init__(self, label, fn=None, write=True, hint=""):
        self.label, self.fn, self.write, self.hint = label, fn, write, hint

    @property
    def sep(self):
        return self.fn is None


class Tui:
    def __init__(self, ctx=None, game_dir=None, lang=None, headless=False, sync=False, io=None):
        self.headless = headless
        self._io = io                  # 测试用: (input, output)
        self.sync = sync               # 测试用: 任务直接在当前线程跑完
        self.lang = lang
        self.err = None
        self.ctx = ctx
        if ctx is None:
            try:
                self.ctx = Ctx.open(game_dir)
            except MDStudioError as e:
                self.err = str(e)
        self.dry = bool(self.ctx and self.ctx.dry_run)   # mdstudio -n 进来就是 dry-run
        self.db = None
        self.names_state = tr("loading")
        self.inv = None
        self.flt = 0
        self.results = []
        self.sel = None                # CardInfo
        self.busy = None
        self._pin = None               # 刚操作过的卡, 搜索结果里钉住它
        self._acts_card = None
        self.modal = None
        self.lines = []
        self.scroll = 0
        self._lock = threading.Lock()
        self._build()

    # ================================ 日志 ================================
    def say(self, text, level=logging.INFO):
        st = "class:err" if level >= logging.ERROR else "class:warn" if level >= logging.WARNING else ""
        with self._lock:
            for ln in str(text).splitlines() or [""]:
                self.lines.append((st, ln))
            del self.lines[:-2000]
        self._redraw()

    def _fail(self, e, what="error"):
        if not isinstance(e, MDStudioError):
            import traceback
            log.debug(traceback.format_exc())
            msg = f"{type(e).__name__}: {e}"
        else:
            msg = str(e)
        self.say(f"{tr(what)}：{msg}", logging.ERROR)

    def _redraw(self):
        try:
            self.app.invalidate()
        except Exception:
            pass

    # ================================ 数据 ================================
    def _boot(self):
        if self.err:
            self.say(f"{tr('error')}：{self.err}", logging.ERROR)
            self.say(tr("Start with:  mdstudio --game \"<Master Duel folder>\""), logging.WARNING)
            self.names_state = "no game"
            return
        self.say(f"{tr('game')}：{self.ctx.game.dir}   {tr('group')} {self.ctx.game.group}")
        if READONLY:
            self.say(tr("READ-ONLY mode: nothing will be written"), logging.WARNING)
        self._reload()
        self._bg(self._load_names, writes=False)

    def _load_names(self):
        from mdstudio.core.cards import CardDB
        try:
            t = time.time()
            self.db = CardDB(self.ctx.game, self.lang).load()
            self.names_state = tr("%s cards") % len(self.db.names)
            if time.time() - t > 2:
                self.say(f"{tr('card names ready')}（{self.names_state}）")
        except Exception as e:   # 读名字失败不影响用 id 操作
            self.names_state = tr("no names (use IDs)")
            self.say(f"{tr('card names unavailable')}：{e}", logging.WARNING)
        self._reload()

    def _reload(self):
        from mdstudio.ops.inventory import Inventory
        if not self.ctx:
            return
        try:
            self.inv = Inventory(self.ctx.game, self.ctx.journal, self.db)
        except Exception as e:
            self._fail(e, "can't read the game's lists")
            return
        self._search()

    def _search(self, *_):
        # 坑: 这个是搜索框 on_text_changed 的回调, 抛出去 = 整个程序退出. 一律进日志
        try:
            self._do_search()
        except Exception as e:
            self._fail(e)

    def _do_search(self):
        inv = self.inv
        if inv is None:
            return
        q = self.search.text.strip() if hasattr(self, "search") else ""
        has = FILTERS[self.flt][0]
        if q.isascii() and q.isdigit() and (not self.db or int(q) not in self.db.names):
            rows = [inv.info(int(q))]          # 没名字的 id 也让用户能操作
            if self.db:
                rows += [ci for ci in inv.search(q, has=has, limit=300) if ci.id != int(q)]
        elif q:
            rows = inv.search(q, has=has, limit=300) if self.db else []
        elif has in ("cutin", "runfx"):
            rows = inv.cards_with(has)
        elif has == "any":
            rows = [inv.info(c) for c in sorted(set(inv.cutin) | set(inv.runfx))]
        else:
            # 什么都没打: 先给"我改过的卡", 懒人第一眼就看到自己的东西
            mine = {r.card for r in self.ctx.journal.load() if r.card}
            rows = [inv.info(c) for c in sorted(mine)]
        cur = self.sel.id if self.sel else None
        if cur is not None and cur == self._pin and cur not in {ci.id for ci in rows}:
            # 刚对这张卡做完操作, 它可能不再符合过滤条件. 别让选中跳到别的卡上 (再按一次回车 = 改错卡)
            rows = [inv.info(cur)] + rows
        self.results = rows
        ids = [ci.id for ci in rows]
        self.cards.idx = ids.index(cur) if cur in ids else 0
        self.sel = rows[self.cards.idx] if rows else None
        self._refresh_acts()
        self._redraw()

    def _pick_card(self, ci):
        if ci is self.sel:
            return
        self._pin = None
        self.sel = ci
        self._refresh_acts()
        self._redraw()

    # ================================ 任务 ================================
    def _bg(self, fn, writes=True):
        if self.sync:
            fn()
        elif not writes:
            # 只读的 (加载卡名 / 刷新) 用 daemon: 冷启动加载要十几秒, 这时退出不该卡住控制台
            threading.Thread(target=fn, daemon=True).start()
        else:
            # 不用 daemon: 写到一半进程退出 = gate/manifest 半成品. 退出时会等它
            threading.Thread(target=fn, daemon=False).start()

    def run_op(self, label, fn, *args, write=True, done=None, **kw):
        """所有改动都从这里走: 新 Ctx (dry-run 按当前开关), 后台跑, 错误进日志不崩"""
        if self.busy:
            self.say(f"{tr('busy with')}：{self.busy}", logging.WARNING)
            return
        if not self.ctx:
            self.say(tr("no game folder"), logging.ERROR)
            return
        if write and READONLY:
            self.say(f"{tr('[read-only] skipped')}：{label}", logging.WARNING)
            return
        self.busy = label
        self._pin = self.sel.id if self.sel else None
        ctx = Ctx(self.ctx.game, dry_run=self.dry, journal=self.ctx.journal,
                  check_running=self.ctx.check_running)
        self.say((tr("[dry-run] ") if self.dry and write else "") + label + "…")

        def job():
            try:
                r = fn(ctx, *args, **kw)
                if done:
                    done(r)
                if write:
                    self.say(tr("done") + (tr(" (dry-run, nothing written)") if self.dry else ""))
            except Exception as e:   # 未知错误也只进日志, 界面别死
                self._fail(e)
            finally:
                self.busy = None
                if write:
                    self._reload()
                self._redraw()
        self._bg(job)

    # ================================ 右边的动作 ================================
    def _refresh_acts(self):
        # 渲染很频繁 (鼠标一动就重画), 动作列表里要读 journal, 所以只在选中/状态变了时算一次
        same_card = (self.sel.id if self.sel else None) == self._acts_card
        prev = self.acts.current()
        try:
            self.act_items = self._actions()
        except Exception as e:
            self.act_items = []
            self._fail(e)
        self._acts_card = self.sel.id if self.sel else None
        if same_card and prev is not None:
            # 坑: 做完一个动作列表变了, 光标回到第一项 = 又是个写操作, 连按回车就改错东西
            labels = [a.label for a in self.act_items]
            if prev.label in labels:
                self.acts.idx = labels.index(prev.label)
                return
            details = tr("Details")
            if details in labels:
                self.acts.idx = labels.index(details)
                return
        self.acts.reset()

    def _actions(self):
        out = []
        ci = self.sel
        if ci is not None:
            nm = ci.name or tr("(no name)")
            out.append(Act(f"── {ci.id}  {nm[:30]} ──"))
            out += self._cutin_actions(ci)
            out += self._runfx_actions(ci)
            if self.ctx and self.ctx.journal.for_card(ci.id):
                out.append(Act(tr("Undo everything this tool did to it"), self.a_rm_card))
            out.append(Act(tr("Details"), self.a_details, write=False))
        out.append(Act("── everything ──"))
        out += [
            Act(tr("My cards (everything this tool changed)"), self.a_mine, write=False),
            Act(tr("Resync after a game update"), self.a_resync),
            Act(tr("Undo a change…"), self.a_undo),
            Act(tr("Export my work as a pack…"), self.a_pack_export),
            Act(tr("Install a pack…"), self.a_pack_install),
            Act(tr("Look inside a pack…"), self.a_pack_inspect, write=False),
            Act(f"{tr('Dry-run')}：{tr('ON') if self.dry else tr('off')}   （F2）", self.a_toggle_dry, write=False),
            Act(f"{tr('Filter')}：{FILTERS[self.flt][1]}   （F3）", self.a_cycle_filter, write=False),
            Act(tr("Refresh   (F5)"), lambda: self._bg(self._reload, writes=False), write=False),
            Act(tr("Quit   (Ctrl+Q)"), self.quit, write=False),
        ]
        return out

    def _cutin_actions(self, ci):
        vid = _has("cv2")
        out = []
        if ci.cutin == "wiped":
            out.append(Act(tr("Put the cut-in back (resync)"), self.a_resync))
            out.append(Act(tr("Forget this cut-in (remove)"), self.a_cutin_remove))
        elif ci.cutin is None:
            if ci.sibling_cutin:
                out.append(Act(f"  （{tr('another printing already has one')}：{', '.join(map(str, ci.sibling_cutin))}）"))
            out.append(Act(tr("Add cut-in: Disaster Leo (default)"), lambda: self.a_cutin(DEFAULT_DONOR)))
            out.append(Act(tr("Add cut-in: pick another card's…"), lambda: self.a_cutin_pick(False)))
            out.append(Act(tr("Add cut-in: editable copy (for reskin)…"), lambda: self.a_cutin_pick(True)))
            out.append(Act(tr("Cut-in from a video / GIF…") if vid else
                           tr("Cut-in from a video  [pip install .[video]]"),
                           self.a_cutin_video if vid else self.a_need_video))
        elif ci.cutin == "game":
            out.append(Act("  " + tr("has a cut-in already")))
            out.append(Act(tr("Replace its cut-in with another card's…"), lambda: self.a_cutin_pick(False, force=True)))
        else:
            from mdstudio.ops import cutin as C
            cloned = self.ctx and C.is_cloned(self.ctx, ci.id)
            out.append(Act(tr("Remove the cut-in"), self.a_cutin_remove))
            out.append(Act(tr("Check its files"), self.a_cutin_verify, write=False))
            if cloned:
                out.append(Act(tr("Reskin: export textures…"), self.a_reskin_export, write=False))
                out.append(Act(tr("Reskin: import painted PNGs…"), self.a_reskin_import))
            if vid:
                out.append(Act(tr("Rebuild from a video / GIF…"), self.a_cutin_video))
        return out

    def _runfx_actions(self, ci):
        vid = _has("cv2")
        out = []
        if ci.runfx == "wiped":
            out.append(Act(tr("Put the effect back (resync)"), self.a_resync))
            out.append(Act(tr("Forget this effect (remove)"), self.a_fx_remove))
        elif ci.runfx is None:
            if ci.sibling_runfx:
                out.append(Act(f"  （{tr('another printing has an effect')}：{', '.join(map(str, ci.sibling_runfx))}）"))
            out.append(Act(tr("Add activation effect: pick a card…"), lambda: self.a_fx_pick(False)))
            out.append(Act(tr("Activation effect from a video…") if vid else
                           tr("Activation effect from a video  [pip install .[video]]"),
                           self.a_fx_video if vid else self.a_need_video))
        elif ci.runfx == "game":
            out.append(Act(tr("Replace its effect with another card's…"), lambda: self.a_fx_pick(True)))
            out.append(Act(tr("Activation effect sound: replace with a WAV…"), self.a_sound_inject))
            out.append(Act(tr("Activation effect sound: restore original…"), self.a_sound_restore))
        else:
            out.append(Act(tr("Remove the activation effect"), self.a_fx_remove))
            if any(r.op == "runfx.video" for r in self.ctx.journal.for_card(ci.id)):
                out.append(Act(tr("Resize the video effect…"), self.a_fx_framing))
            out.append(Act(tr("Activation effect sound: replace with a WAV…"), self.a_sound_inject))
            out.append(Act(tr("Activation effect sound: restore original…"), self.a_sound_restore))
        return out

    def _on_act(self, act):
        if act is None or act.sep:
            return
        if self.busy and act.write:
            self.say(f"{tr('busy with')}：{self.busy}", logging.WARNING)
            return
        try:
            act.fn()
        except Exception as e:   # 按键回调里抛出去 = 整个界面退出, 一律进日志
            self._fail(e)

    # ---- cut-in ----
    def _name(self, cid):
        return (self.db.name(cid) if self.db else "") or str(cid)

    def a_cutin(self, donor, clone=False, force=False):
        from mdstudio.ops import cutin as C
        ci = self.sel
        fn = C.clone if clone else C.borrow
        self.run_op(tr("cut-in for %s %s from %s %s") % (ci.id, self._name(ci.id), donor, self._name(donor)),
                    lambda ctx: (fn(ctx, ci.id, donor, force=force), self._verify(ctx, ci.id)))

    def a_cutin_pick(self, clone, force=False):
        inv = self.inv
        donors = sorted((c for c, st in inv.cutin.items() if st == "game"), key=lambda c: self._name(c).lower())
        donors.sort(key=lambda c: c != DEFAULT_DONOR)
        rows = [(c, f"{c:>6}  {self._name(c)}") for c in donors]

        def go(donor):
            self.a_cutin(donor, clone=clone, force=force)
        self._choose(tr("Copy the cut-in of which card?"), rows, go,
                     hint=tr("%s cards have one. Type to filter.") % len(rows), searchable=True)

    def _verify(self, ctx, card, force=False):
        if ctx.dry_run and not force:      # dry-run 的 add 根本没写文件, 查了也是缺
            return
        from mdstudio.core.catalog import Catalog
        from mdstudio.ops import cutin as C
        miss = C.missing(Catalog(ctx.game), card)
        for m in miss:
            self.say(f"{tr('MISSING')} {m}", logging.ERROR)
        self.say(tr("files OK. Summon it in Solo to see it.") if not miss
                 else tr("missing files = infinite load in game. Remove it."),
                 logging.INFO if not miss else logging.ERROR)

    def a_cutin_remove(self):
        from mdstudio.ops import cutin as C
        ci = self.sel
        self._confirm(tr("Remove the cut-in this tool added to %s %s?") % (ci.id, self._name(ci.id)),
                      lambda: self.run_op(f"{tr('remove cut-in')} {ci.id}", C.remove, ci.id))

    def a_cutin_verify(self):
        self.run_op(f"{tr('check')} {self.sel.id}", self._verify, self.sel.id, force=True, write=False)

    def a_cutin_video(self):
        from mdstudio.ops.video import cutin_flip
        ci = self.sel
        # 默认整段原样全屏 (Phainon 就是这么做的), 抠图要自己选
        modes = [("full", tr("Full screen, the whole video as is (nothing cut)")),
                 ("square", tr("Square crop from the middle")),
                 ("matte", tr("AI cut-out: character only, background removed") if _has("rembg") else
                  tr("AI cut-out: character only  [needs rembg]"))]

        def got_path(p):
            def got_mode(mode):
                def got_dur(text):
                    dur = _num(text, 4.8, 0.3, 15.0)
                    self.run_op(f"{tr('cut-in from video for')} {ci.id}（{mode}，{dur} {tr('seconds')}）",
                                cutin_flip.apply, ci.id, p, mode=mode, duration=dur)
                self._input(tr("Length in seconds"), "4.8", got_dur,
                            hint=tr("The whole video is played in this time. The cut-in Timeline is stretched to fit."))
            self._choose(tr("How should the video look?"), modes, got_mode)
        self._ask_path(tr("Video, GIF or frame folder"), got_path)

    def a_reskin_export(self):
        from mdstudio.ops import cutin as C
        ci = self.sel
        default = os.path.join(os.path.expanduser("~"), "Desktop", f"reskin_{ci.id}")
        self._input(tr("Export textures to which folder?"), default,
                    lambda d: self.run_op(f"{tr('export textures of')} {ci.id}", C.reskin_export, ci.id, pick.path(d),
                                          write=False, done=lambda fs: self.say(f"{len(fs)} {tr('file(s)')} -> {d}")))

    def a_reskin_import(self):
        from mdstudio.ops import cutin as C
        ci = self.sel

        def got(p):
            files = sorted(p.glob("*.png")) if p.is_dir() else [p]
            self.run_op(f"{tr('import')} {len(files)} {tr('PNG(s) into')} {ci.id}", C.reskin_import, ci.id, files,
                        done=lambda n: self.say(tr("%s texture(s) replaced") % n))
        self._ask_path(tr("Folder with the painted PNGs (or one PNG)"), got)

    # ---- 发动效果 ----
    def a_fx_pick(self, replace):
        from mdstudio.ops import runfx as R
        ci = self.sel
        rows, seen = [], set()
        for m, t, se, ep, ok in R.list_effects(self.ctx.game):
            if m in seen or ok is False:
                continue
            seen.add(m)
            rows.append((m, f"{m:>6}  {self._name(m)[:34]:<34}  {tr(t) or '':<8} {se or ''}"))
        rows.sort(key=lambda r: self._name(r[0]).lower())
        self._choose(tr("Copy the activation effect of which card?"), rows,
                     lambda donor: self.run_op(tr("effect for %s from %s %s") % (ci.id, donor, self._name(donor)),
                                               self._fx_borrow, ci.id, donor, replace),
                     hint=tr("Effect + sound. Type to filter. Best on one-effect cards, and pick a "
                             "'chain' donor: it plays on every activation."), searchable=True)

    @staticmethod
    def _fx_borrow(ctx, card, donor, replace):
        from mdstudio.ops import runfx as R
        R.borrow(ctx, card, donor, replace=replace)

    def a_fx_remove(self):
        from mdstudio.ops import runfx as R
        ci = self.sel
        self._confirm(tr("Remove the effect this tool added to %s %s?") % (ci.id, self._name(ci.id)),
                      lambda: self.run_op(f"{tr('remove effect')} {ci.id}", R.remove, ci.id))

    def a_fx_video(self):
        from mdstudio.ops import sound as S
        from mdstudio.ops import runfx as R
        from mdstudio.ops.video import runfx_flip
        ci = self.sel
        labels = sorted({se for _m, _t, se, _ep, _ok in R.list_effects(self.ctx.game)
                         if se and S.on_disk(self.ctx.game, se)})
        carrier = runfx_flip.DEFAULT_CARRIER if runfx_flip.DEFAULT_CARRIER in labels else (labels[:1] or [""])[0]
        # 第一项 = 用视频自带的声音 (默认), 借 carrier 这个 label 装
        sounds = ([(("video", carrier), tr("The video's own audio (carried on %s)") % carrier)] if carrier else []) + \
                 [(("", ""), tr("No sound"))] + [(("", lb), f"{lb} {tr('as it is')}") for lb in labels]

        def got_path(p):
            def got_sound(pick_):
                use, se = pick_[0] == "video", pick_[1]

                def got_dur(text):
                    dur = _num(text, 5.0, 0.5, 30.0)
                    self.run_op(f"{tr('effect from video for')} {ci.id}（{dur} {tr('seconds')}）",
                                runfx_flip.apply, ci.id, p, duration=dur, se_label=se, video_audio=use)
                self._input(tr("Length in seconds"), "5", got_dur,
                            hint=tr("Longer = fewer frames per second (sheet holds 120 frames at 1080p)."))
            self._choose(tr("Which sound plays with it?"), sounds, got_sound,
                         hint=tr("New sound labels are silent, so the video's audio rides on an existing one "
                                 "(cards using it play it too)."), searchable=True)
        self._ask_path(tr("Video file"), got_path)

    def a_fx_framing(self):
        from mdstudio.ops.video import runfx_flip
        ci = self.sel
        self._input(tr("Height on screen (1.0 = full height, fullscreen 16:9 = 1.08)"), "1.08",
                    lambda t: self.run_op(f"{tr('resize effect of')} {ci.id}", runfx_flip.set_framing, ci.id,
                                          _num(t, 1.08, 0.05, 4.0)))

    # ---- 声音 (只管 run effect, cut-in 的声音不归这里) ----
    def _labels(self, card):
        from mdstudio.core.gate import RunFxGate
        from mdstudio.ops import sound as S
        return [(lb, lb + ("" if S.on_disk(self.ctx.game, lb) else
                           "   " + tr("(built into the game, can't replace)") if lb in S.BUILTIN
                           else "   " + tr("(not on disk)")))
                for lb in S.labels_of_card(RunFxGate(self.ctx.game), card)]

    def a_sound_inject(self):
        from mdstudio.ops import sound as S
        rows = self._labels(self.sel.id)
        if not rows:
            self.say(tr("this card's activation effect plays no sound"), logging.WARNING)
            return

        def got_label(lb):
            self._ask_path(tr("WAV file"), lambda p: self.run_op(f"{tr('effect sound')} {lb} <- {p.name}", S.inject, lb, p,
                                                             done=lambda _r: self.say(f"{tr('tip')}：" + S.loudness_hint())))
        self._choose(tr("Replace which activation-effect sound?"), rows, got_label,
                     hint=tr("Every card whose activation effect uses this sound changes too."))

    def a_sound_restore(self):
        from mdstudio.ops import sound as S
        rows = self._labels(self.sel.id)
        if not rows:
            self.say(tr("this card's activation effect plays no sound"), logging.WARNING)
            return
        self._choose(tr("Restore which activation-effect sound?"), rows,
                     lambda lb: self.run_op(f"{tr('restore effect sound')} {lb}", S.restore, lb))

    # ---- 全局 ----
    def a_details(self):
        ci = self.inv.info(self.sel.id)
        self.say(f"{ci.id}  {ci.name or tr('(no name)')}")
        self.say(f"  {tr('cut-in')}：{_txt(ci.cutin)}    {tr('effect')}：{_txt(ci.runfx)}")
        for t, se, ep in ci.effects:
            self.say(f"  {tr(t) or '':<9} {tr('sound')} {se or '-'}  {ep or tr('(built-in)')}")
        if self.db:
            sib = [s for s in self.db.same_name(ci.id) if s != ci.id]
            if sib:
                self.say("  " + tr("same name") + "：" + ", ".join(
                    f"{s}（{tr('cut-in')} {_txt(self.inv.cutin.get(s))}，{tr('effect')} {_txt(self.inv.runfx.get(s))}）"
                    for s in sib))
        for r in self.ctx.journal.for_card(ci.id):
            self.say(f"  {tr('this tool')}：{_when(r.time)}  {r.op}  {r.meta.get('donor') or ''}")

    def a_mine(self):
        self.search.text = ""
        self.flt = 0
        self._search()
        self.say(tr("%s card(s) changed by this tool") % len(self.results) if self.results else
                 tr("nothing yet. Search a card on the left, then pick an action here."))

    def a_rm_card(self):
        from mdstudio.ops.tx import undo_ids
        ci = self.sel
        recs = self.ctx.journal.for_card(ci.id)
        self._confirm(tr("Undo %s change(s) on %s %s?") % (len(recs), ci.id, self._name(ci.id)),
                      lambda: self.run_op(tr("undo all on %s") % ci.id, undo_ids, [r.id for r in recs]),
                      hint=", ".join(r.op for r in recs))

    def a_undo(self):
        from mdstudio.ops.tx import undo_ids
        recs = sorted(self.ctx.journal.load(), key=lambda r: -r.time)
        if not recs:
            self.say(tr("nothing to undo"))
            return
        rows = [(r.id, f"{_when(r.time)}  {r.op:<13} {r.card or '':>6} {self._name(r.card)[:28] if r.card else ''}")
                for r in recs]
        self._choose(tr("Undo which change? (newest first)"), rows,
                     lambda rid: self.run_op(f"{tr('undo')} {rid}", undo_ids, [rid]), searchable=True)

    def a_resync(self):
        from mdstudio.ops.tx import resync

        def show(r):
            self.say(f"{tr('cut-ins back')}：{r['cutin'] or '-'}   {tr('effects back')}：{r['runfx'] or '-'}   "
                     f"{tr('manifest fixed')}：{r['catalog']}")
            for f in r["lost_files"]:
                self.say(f"{tr('LOST')} {f}（{tr('make that card again')}）", logging.ERROR)
            for c in r.get("broken", []):
                self.say(f"{tr('BROKEN')} {c}（{tr('remove it and add it again')}）", logging.ERROR)
            for c in r.get("game_took_over", []):
                self.say(f"{c}：{tr('the game now has its own version, yours was left off')}", logging.WARNING)
            for lb in r.get("sound_reset", []):
                self.say(f"{tr('effect sound')} {lb} {tr('was updated by the game, your WAV is gone (restore it, then replace again)')}",
                         logging.WARNING)
        self.run_op(tr("resync"), resync, done=show)

    def a_pack_export(self):
        from mdstudio.ops import pack as P
        cards = sorted(P.exportable(self.ctx.journal))
        if not cards:
            self.say(tr("nothing to export yet"), logging.WARNING)
            return

        def got_name(name):
            default = os.path.join(os.path.expanduser("~"), "Desktop")
            self._input(tr("Save to which folder?"), default,
                        lambda d: self.run_op(tr("export %s card(s)") % len(cards), P.export, cards, pick.path(d),
                                              name=name.strip() or "my-pack", write=False,
                                              done=lambda path: self.say(f"{tr('pack')} -> {path}")))
        self._input(f"{tr('Pack name')}（{len(cards)} {tr('card(s), every dependency included')}）", "my-pack", got_name)

    def a_pack_install(self):
        from mdstudio.ops import pack as P
        self._ask_path(tr("Pack (.zip or folder)"), lambda p: self.run_op(
            f"{tr('install')} {p.name}", P.install, p, done=lambda r: self.say(f"{tr('installed')}：{r or tr('nothing new')}")))

    def a_pack_inspect(self):
        from mdstudio.ops import pack as P

        def got(p):
            m = P.inspect(p)
            self.say(f"{m['name']}  （{m['tool']}）")
            for c in m["cards"]:
                self.say(f"  {c['kind']:<6} {c['mrk']:>6}  {self._name(c['mrk'])}")
        self._ask_path(tr("Pack (.zip or folder)"), got)

    def a_toggle_dry(self):
        self.dry = not self.dry
        self._refresh_acts()
        self.say(f"{tr('dry-run')} {tr('ON: actions only print what they would change') if self.dry else tr('off')}")

    def a_cycle_filter(self):
        self.flt = (self.flt + 1) % len(FILTERS)
        self._search()

    def a_need_video(self):
        self.say(tr("video needs extra packages:  pip install .[video]   (opencv, rembg)"), logging.WARNING)

    # ================================ 弹窗 ================================
    def _close(self):
        self.modal = None
        self._focus(self.acts.window)
        self._redraw()

    def _focus(self, w):
        try:
            self.app.layout.focus(w)
        except Exception:
            pass

    def _btn(self, label, fn, style="class:btn"):
        def h(ev):
            if ev.event_type == MouseEventType.MOUSE_UP:
                fn()
                return None
            return NotImplemented
        # 中文全角占两列, 窗口宽度按显示宽度算, 不然按钮被裁一半
        return Window(FormattedTextControl([(style, f" {label} ", h)]), height=1, width=_w(label) + 2,
                      dont_extend_width=True)

    def _show(self, title, body, hint, keys, focus):
        rows = [Label(title, style="class:mtitle")]
        if hint:
            rows.append(Label(hint, style="class:dim"))
        rows += body + [Label(keys, style="class:dim")]
        self.modal = Frame(HSplit(rows), style="class:modal", width=D(min=50, preferred=90, max=120))
        self._focus(focus)
        self._redraw()

    def _input(self, title, default, on_ok, hint=""):
        ta = TextArea(text=default or "", multiline=False, style="class:input", focus_on_click=True)
        ta.buffer.cursor_position = len(ta.text)

        def ok():
            v = ta.text
            self._close()
            try:
                on_ok(v)
            except Exception as e:
                self._fail(e)

        kb = KeyBindings()
        kb.add("enter")(lambda e: ok())
        kb.add("escape", eager=True)(lambda e: self._close())
        ta.control.key_bindings = kb
        self._show(title, [Frame(ta), VSplit([self._btn(tr("OK"), ok, "class:btn.ok"), Window(width=2),
                                              self._btn(tr("Cancel"), self._close), Window()], height=1)],
                   hint, tr("Enter = OK · Esc = cancel"), ta)

    def _ask_path(self, title, on_ok):
        def got(text):
            p = pick.path(text)
            if not p.exists():
                raise MDStudioError(tr("not found: %s") % p)
            on_ok(p)
        self._input(title, "", got, hint=tr("Drag the file into this window, or paste its path."))

    def _choose(self, title, options, on_ok, hint="", searchable=False):
        """options = [(value, text)]. searchable = 上面加个过滤框"""
        state = {"rows": list(options)}
        flt = TextArea(multiline=False, prompt=tr("filter: "), style="class:input", focus_on_click=True) \
            if searchable else None

        def activate(it):
            if it is None:
                return
            self._close()
            try:
                on_ok(it[0])
            except Exception as e:
                self._fail(e)

        cl = ClickList(lambda: state["rows"], lambda it: it[1], on_activate=activate, click_activates=True,
                       empty=tr("(no match)"), on_escape_up=(lambda: self._focus(flt)) if flt else None)
        cl.app = self.app
        kb = KeyBindings()
        kb.add("escape", eager=True)(lambda e: self._close())
        cl.control.key_bindings = merge_key_bindings([cl.control.key_bindings, kb])
        body = [Frame(cl.window, height=D(min=3, max=18))]
        focus = cl.window
        if flt:
            def changed(_):
                words = fold(flt.text).split()
                state["rows"] = [o for o in options if all(w in fold(o[1]) for w in words)]
                cl.idx = 0
            flt.buffer.on_text_changed += changed
            fkb = KeyBindings()
            fkb.add("down")(lambda e: self._focus(cl.window))
            fkb.add("enter")(lambda e: activate(cl.current()))
            fkb.add("escape", eager=True)(lambda e: self._close())
            flt.control.key_bindings = fkb
            body.insert(0, flt)
            focus = flt
        self._show(title, body, hint, tr("type to filter · ↑↓ / click · Enter = pick · Esc = cancel")
                   if flt else tr("↑↓ / click · Enter = pick · Esc = cancel"), focus)
        return cl

    def _confirm(self, title, on_yes, hint=""):
        def yes():
            self._close()
            try:
                on_yes()
            except Exception as e:
                self._fail(e)
        yb = self._btn(tr("Yes"), yes, "class:btn.ok")
        yb.content.focusable = lambda: True
        kb = KeyBindings()
        kb.add("y")(lambda e: yes())
        kb.add("enter")(lambda e: yes())
        kb.add("n")(lambda e: self._close())
        kb.add("escape", eager=True)(lambda e: self._close())
        yb.content.key_bindings = kb
        self._show(title, [VSplit([yb, Window(width=2), self._btn(tr("No"), self._close), Window()], height=1)],
                   hint, tr("Y / Enter = yes · N / Esc = no"), yb)

    # ================================ 画面 ================================
    def _row(self, ci):
        cut, cs = BADGE.get(ci.cutin, ("-", "class:dim"))
        fx, fs = BADGE.get(ci.runfx, ("-", "class:dim"))
        out = [("class:cid", f"{ci.id:>6} "), (cs, f"{cut:<6}"), (fs, f"{fx:<6}"), ("", ci.name or tr("(no name)"))]
        note = []
        if not ci.cutin and ci.sibling_cutin:
            note.append(tr("cut-in on %s") % ",".join(map(str, ci.sibling_cutin)))
        if not ci.runfx and ci.sibling_runfx:
            note.append(tr("effect on %s") % ",".join(map(str, ci.sibling_runfx)))
        if note:
            out.append(("class:dim", "   （" + "; ".join(note) + "）"))
        return out

    def _act_row(self, act):
        if act.sep:
            return [("class:sep", act.label)]
        return [("class:ro" if act.write and READONLY else "", act.label)]

    def _head(self):
        return [("class:dim", "    id  " + tr("cut") + "  " + tr("fx") + "   " + tr("name"))]

    def _legend(self):
        return [("class:b.game", " " + tr("yes", ctx="badge")), ("class:dim", " " + tr("game or another mod") + "  "),
                ("class:b.yours", tr("yours")), ("class:dim", " " + tr("made by this tool") + "  "),
                ("class:b.wiped", tr("WIPED")), ("class:dim", " " + tr("update removed it"))]

    def _log_view(self):
        with self._lock:
            n = len(self.lines)
            end = max(0, n - self.scroll)
            rows = self.lines[max(0, end - LOG_ROWS):end]
        h = self._log_mouse
        if not rows:
            return [("class:dim", "  " + tr("search a card on the left (name or ID), then pick what to do on the right"), h)]
        out = []
        for st, t in rows:
            out.append((st, (t if len(t) < 220 else t[:219] + "…") + "\n", h))
        if self.scroll:
            out.append(("class:dim", f"  ↓ {self.scroll} {tr('newer line(s)')}", h))
        return out

    def _log_mouse(self, ev):
        if ev.event_type == MouseEventType.SCROLL_UP:
            self.scroll = min(max(0, len(self.lines) - LOG_ROWS), self.scroll + 3)
            return None
        if ev.event_type == MouseEventType.SCROLL_DOWN:
            self.scroll = max(0, self.scroll - 3)
            return None
        return NotImplemented

    def _status(self):
        parts = []
        if self.ctx:
            parts.append(f"✓ {tr('group')} {self.ctx.game.group}")
        else:
            parts.append("✗ " + tr("no game"))
        parts.append(self.names_state)
        parts.append(f"{tr('filter')}：{FILTERS[self.flt][1]}")
        if self.dry:
            parts.append(tr("DRY-RUN"))
        if READONLY:
            parts.append(tr("READ-ONLY"))
        if self.busy:
            parts.append(f"{tr('working')}：{self.busy[:50]}…")
        left = "  ·  ".join(parts)
        keys = tr("Tab panel · / search · F2 dry-run · F3 filter · F5 refresh · Ctrl+Q quit")
        return [("class:status.busy" if self.busy else "class:status", f" {left} "),
                ("class:status.keys", f"  {keys} ")]

    def _build(self):
        self.search = TextArea(multiline=False, prompt=" " + tr("Search") + "：", style="class:search", focus_on_click=True)
        self.search.buffer.on_text_changed += self._search
        skb = KeyBindings()
        skb.add("down")(lambda e: self._focus(self.cards.window))
        skb.add("enter")(lambda e: self._focus(self.acts.window if self.sel else self.cards.window))
        skb.add("escape", eager=True)(lambda e: self._close() if self.modal else setattr(self.search, "text", ""))
        self.search.control.key_bindings = skb

        self.cards = ClickList(lambda: self.results, self._row, on_select=self._pick_card,
                               on_activate=lambda ci: (self._pick_card(ci), self._focus(self.acts.window)),
                               on_escape_up=lambda: self._focus(self.search),
                               empty=tr("type a card name or ID above (empty = cards you changed)"))
        self.act_items = []
        self.acts = ClickList(lambda: self.act_items, self._act_row, on_activate=self._on_act, click_activates=True,
                              selectable=lambda a: not a.sep)

        left = Frame(HSplit([self.search, Window(FormattedTextControl(self._head), height=1),
                             self.cards.window, Window(FormattedTextControl(self._legend), height=1)]),
                     title=tr("Cards"))
        right = Frame(self.acts.window, title=tr("What to do"), width=D(min=36, preferred=48))
        logw = Frame(Window(FormattedTextControl(self._log_view), height=D.exact(LOG_ROWS), wrap_lines=False,
                            style="class:log"), title=tr("Log"))
        status = Window(FormattedTextControl(self._status), height=1, style="class:status")
        root = FloatContainer(
            HSplit([Window(FormattedTextControl([("class:title", f" MD Cut-in Kit {__version__} ")]), height=1),
                    VSplit([left, right]), logw, status]),
            floats=[Float(DynamicContainer(lambda: self.modal or Window(height=0)))])

        extra = {}
        if self._io:
            extra = {"input": self._io[0], "output": self._io[1]}
        elif self.headless:
            from prompt_toolkit.input import DummyInput
            from prompt_toolkit.output import DummyOutput
            extra = {"input": DummyInput(), "output": DummyOutput()}
        self.app = Application(layout=Layout(root, focused_element=self.search), key_bindings=self._kb(),
                               style=_style(), mouse_support=True, full_screen=True, **extra)
        self.cards.app = self.acts.app = self.app
        self.cards.blocked = self.acts.blocked = lambda: self.modal is not None

    def _kb(self):
        kb = KeyBindings()
        free = Condition(lambda: self.modal is None)
        typing = has_focus(self.search)

        @kb.add("c-c", eager=True)
        @kb.add("c-q", eager=True)
        def _(e):
            self.quit()

        kb.add("escape", filter=~free)(lambda e: self._close())

        @kb.add("<any>", filter=free & has_focus(self.cards.window))
        def _(e):
            # 在卡片列表上直接打字 = 搜索, 不用先点搜索框
            ch = e.data
            if len(ch) == 1 and ch.isprintable():
                self._focus(self.search)
                self.search.buffer.insert_text(ch)

        kb.add("/", filter=free & ~typing)(lambda e: self._focus(self.search))
        kb.add("tab", filter=free)(lambda e: self._cycle(1))
        kb.add("s-tab", filter=free)(lambda e: self._cycle(-1))
        kb.add("f2", filter=free)(lambda e: (self.a_toggle_dry(), self._redraw()))
        kb.add("f3", filter=free)(lambda e: self.a_cycle_filter())
        kb.add("f5", filter=free)(lambda e: self._bg(self._reload, writes=False))
        kb.add("pageup", filter=~has_focus(self.cards.window) & ~has_focus(self.acts.window))(
            lambda e: self._log_mouse(_Scroll(MouseEventType.SCROLL_UP)))
        kb.add("pagedown", filter=~has_focus(self.cards.window) & ~has_focus(self.acts.window))(
            lambda e: self._log_mouse(_Scroll(MouseEventType.SCROLL_DOWN)))
        return kb

    def quit(self):
        if self.busy:
            self.say(f"{tr('still writing')}（{self.busy}）。" + tr("Quit when it's done, or the change is left half applied."),
                     logging.WARNING)
            return
        self.app.exit()

    def _cycle(self, d):
        order = [self.search, self.cards.window, self.acts.window]
        cur = next((i for i, w in enumerate(order) if self.app.layout.has_focus(w)), 0)
        self._focus(order[(cur + d) % len(order)])

    def run(self):
        h = _ToLog(self)
        old = log.handlers[:], log.level, log.propagate
        log.handlers[:] = [h]
        log.setLevel(logging.INFO)
        log.propagate = False
        try:
            self._boot()
            self.app.run()
        finally:
            log.handlers[:], log.level, log.propagate = old


class _Scroll:
    def __init__(self, t):
        self.event_type = t


def _txt(st):
    return {"game": tr("yes", ctx="badge"), "yours": tr("yours"), "wiped": tr("WIPED")}.get(st, "-")


def _when(t):
    return time.strftime("%m-%d %H:%M", time.localtime(t))


def _num(text, default, lo, hi):
    try:
        v = float(str(text).strip().replace(",", "."))
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _style():
    return Style.from_dict({
        "title": "bg:#3a5fcd #ffffff bold",
        "frame.border": "#5f5f87",
        "frame.label": "#ffd75f bold",
        "search": "bg:#1c1c28 #ffffff",
        "sel": "bg:#3a5fcd #ffffff bold",
        "sel.blur": "bg:#2a2a3c #d7d7e6",
        "dim": "#7a7a8c",
        "sep": "#8787af bold",
        "ro": "#6a6a7a",
        "cid": "#5fafff",
        "b.game": "#5fd787",
        "b.yours": "#5fd7ff bold",
        "b.wiped": "#ff5f5f bold",
        "log": "#c8c8d4",
        "err": "#ff6b6b",
        "warn": "#ffaf5f",
        "status": "bg:#26263a #d0d0e0",
        "status.busy": "bg:#875f00 #ffffff bold",
        "status.keys": "bg:#1c1c28 #8a8aa0",
        "modal": "bg:#1a1a2a",
        "mtitle": "#ffd75f bold",
        "input": "bg:#101018 #ffffff",
        "btn": "bg:#3a3a4a #ffffff",
        "btn.ok": "bg:#2e7d32 #ffffff bold",
    })


def run(ctx=None, game_dir=None, lang=None):
    Tui(ctx=ctx, game_dir=game_dir, lang=lang).run()
