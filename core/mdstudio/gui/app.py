"""GUI. 只负责收参数 + 丢到后台跑 ops, 逻辑全在 ops 里"""
import logging
import os
import sys
import time

from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSpinBox,
                               QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from mdstudio import __build__, __version__
from mdstudio import settings
from mdstudio.core.context import Ctx
from mdstudio.core.game import Game, detect_game_dir, list_accounts
from mdstudio.errors import MDStudioError
from mdstudio.gui import collapsible, pill
from mdstudio.gui import schemes as SCH
from mdstudio.gui import segmented, theme, wheelguard
from mdstudio.gui.worker import Job, LogBridge, ProcJob, picklable
from mdstudio.gui.window.nav import Nav
from mdstudio.gui.window.titlebar import MicaWindow
from mdstudio import progress
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")

# 弹出缩放的预设曲线. 参数不对外展示, 界面上只给名字 (选「自定义」才出现输入框)
POP_CURVE_SILVICOLA = ("0,10,10<7,40,45<14,65,72<21,85,90<27,99,96<32,110,100"
                       "<40,75,100<46,85,100<52,80,100<68,82,100")

# 源卡预设: 每类挑结构最简的官方动画 (骨骼越少越不容易出事), 数据来自 2026-10-24 全量扫描
DONOR_PRESETS = (
    ("Normal / Effect", 3868),       # 欧贝利斯克之巨神兵 19 骨 1 页
    ("Fusion", 17443),               # 元素－英雄 闪耀新宇翼人 21 骨 3 页
    ("Ritual", 11377),               # 崇光之宣告者 67 骨 2 页
    ("Synchro", 7698),               # 巨人斗士 15 骨 2 页
    ("Xyz", 11016),                  # 混沌编号88 灾难狮子 2 骨 2 页 (工具底板基准)
    ("Pendulum", 18478),             # 眠梦中的妮梦雷莉娅 21 骨 1 页
    ("Link", 15036),                 # 到临电子界＠伊格尼斯者 16 骨 3 页
)

# 状态栏上的任务名. 没列的用函数名
JOB_TITLES = {
    "cutin.borrow": tr("Borrowing a cut-in"), "cutin.clone": tr("Cloning a cut-in"),
    "cutin.remove": tr("Removing a cut-in"),
    "cutin.reskin_export": tr("Exporting textures"), "cutin.reskin_import": tr("Importing textures"),
    "cutin_flip.apply": tr("Cut-in from a video"), "runfx_flip.apply": tr("Activation effect from a video"),
    "runfx_flip.set_framing": tr("Resizing the effect"), "runfx.borrow": tr("Borrowing an activation effect"),
    "runfx.remove": tr("Removing an activation effect"), "sound.inject": tr("Replacing an effect sound"),
    "sound.restore": tr("Restoring an effect sound"), "pack.export": tr("Exporting a pack"),
    "pack.install": tr("Installing a pack"),
    "tx.resync": tr("Resync after update"), "tx.undo_ids": tr("Undo"),
    "spine.port_card": tr("Importing a Spine cut-in"), "spine.port_all": tr("Importing every Spine cut-in"),
    "spine.check_all": tr("Checking Spine cut-ins"),
}


def default_carrier(labels):
    # Phainon 那次借的就是这个 (Droll & Lock Bird 的), 盘上没有就随便拿第一个
    from mdstudio.ops.video.runfx_flip import DEFAULT_CARRIER
    return DEFAULT_CARRIER if DEFAULT_CARRIER in labels else (labels[0] if labels else "")


def _switch_and_inject(ctx, card, label, wav):
    """Replace 按钮: 选中的卡是自己加的特效, 而且现在没用这个 label -> 先让它用上, 再换 WAV"""
    from mdstudio.core.gate import RunFxGate
    from mdstudio.ops import runfx as R
    from mdstudio.ops import sound as S
    import tempfile
    from pathlib import Path
    from mdstudio.ops.tx import undo
    gate = RunFxGate(ctx.game)
    if card and R._ours(ctx, card) and label not in S.labels_of_card(gate, card):
        R.set_sound(ctx, card, label)
    prev = [r for r in ctx.journal.load() if r.op == "sound.inject" and r.meta.get("label") == label]
    if prev and not ctx.dry_run:
        log.info(tr("%s already had a WAV from this tool, replacing it"), label)
        undo(ctx, prev)
    if Path(wav).suffix.lower() == ".wav":
        S.inject(ctx, label, wav, card=card or 0)
        return
    # 给的是视频: 抽它的声音, 长度对齐这张卡特效的长度 (finishSecond = 时长 + 0.8)
    from mdstudio.ops.video import audio
    fins = [e.get("finishSecond", 0) for e in gate.for_card(card)] if card else []
    dur = max(fins) - 0.8 if fins and max(fins) > 1 else None
    with tempfile.TemporaryDirectory() as d:
        S.inject(ctx, label, audio.extract(wav, Path(d) / "sound.wav", duration=dur), card=card or 0)


JOB_TITLES["app._switch_and_inject"] = tr("Replacing an effect sound")

# 这些要压大贴图 / 读写大文件, 在子进程跑 (见 worker._child)
HEAVY = {"cutin_flip.apply", "runfx_flip.apply", "runfx_flip.set_framing", "app._switch_and_inject",
         "pack.export", "pack.install", "cutin.reskin_import", "cutin.reskin_export", "sound.inject",
         "spine.port_card", "spine.port_all", "spine.check_all"}


def _job_key(fn):
    return f"{getattr(fn, '__module__', '').rsplit('.', 1)[-1]}.{getattr(fn, '__name__', '')}"


def _job_title(fn):
    return JOB_TITLES.get(_job_key(fn), fn.__name__.replace("_", " ").capitalize())


def _clock(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"

STATUS_COLOR = {"yours": "#3fb27f", "wiped": "#e0a030", "game": "#8a8f98",
                "official": "#c42b1c"}      # official = 目标卡官方自带, 克隆会覆盖它
# game = 游戏表里本来就有 (官方或别的 mod), 区分不了, 统一叫 yes
# ctx="badge": 和 CLI/TUI 的徽章共用一套译文
STATUS_TEXT = {"yours": tr("yours"), "wiped": tr("WIPED"), "game": tr("Has a game cut-in"), None: "-"}


VIDEO_MODES = [("full", tr("Full screen — whole video as is, nothing cut")),
               ("square", tr("Square — crop the middle")),
               ("matte", tr("AI cut-out — character only, background removed (slow)"))]


def _mode_box(modes=None):
    box = QComboBox()
    for key, text in (modes or VIDEO_MODES):
        box.addItem(text, key)
    box.setCurrentIndex(0)
    return box


class _ScrollBody(QWidget):
    """滚动区里的内容容器

    坑: QScrollArea 的 widgetResizable 只保证不小于 minimumSizeHint, 而 QGroupBox
    的最小高很小, 于是窗口一矮, QVBoxLayout 就把卡片压扁, 卡片里的说明文字溢出到
    下一张卡片的标题上 (实测「克隆…」那句会盖住「从视频 / GIF / 帧文件夹」).
    这里把最小高度顶到内容实际需要的高度, 窗口不够高就出滚动条, 不再压。
    """

    def minimumSizeHint(self) -> QSize:
        base = super().minimumSizeHint()
        lay = self.layout()
        if lay is None:
            return base
        need = max(lay.sizeHint().height(), lay.minimumSize().height())
        return QSize(base.width(), max(base.height(), need))


def _scroll(layout):
    """右边那列东西多, 窗口矮的时候 Qt 会把控件压扁 (字被切一半). 包一层滚动区, 不压, 出滚动条"""
    from PySide6.QtWidgets import QFrame, QScrollArea
    inner = _ScrollBody()
    inner.setLayout(layout)
    area = QScrollArea()
    area.setWidget(inner)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)   # 窗口拖窄时给横向滚动条, 不要偷偷把内容裁掉
    # 竖滚动条常显: 内容超出而滚动条隐身时, 用户会以为下面没有东西了
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
    return area


def _file_count(game_dir, account) -> int:
    """账号目录下的文件数, 用来区分两个账号 (文件多的一般是常用的那个)"""
    from pathlib import Path
    root = Path(game_dir) / "LocalData" / account / "0000"
    try:
        return sum(len(os.listdir(d)) for d in root.iterdir() if d.is_dir())
    except OSError:
        return 0


def _cell(val, tip=None):
    it = QTableWidgetItem(str(val))
    it.setToolTip(str(val) if tip is None else str(tip))   # 列窄的时候鼠标停上去看全文
    return it


def _list_card(title: str, lst: QListWidget) -> QWidget:
    """左栏那种「标题 + 列表」的卡片

    标题放在卡片内部 (跟右侧的分组卡片一致), 两栏的卡片上沿才能对齐;
    列表自己不要边框, 否则卡片里又套一层卡片.
    """
    card = QWidget()
    card.setObjectName("Card")
    v = QVBoxLayout(card)
    v.setContentsMargins(12, 10, 12, 12)
    v.setSpacing(8)
    head = QLabel(title)
    head.setObjectName("CardTitle")
    v.addWidget(head)
    lst.setObjectName("Flat")
    v.addWidget(lst, 1)
    return card


def _fit_columns(tbl):
    """列宽按内容, 最后一列填满; 太宽就出横向滚动条, 不截断名字.
    坑: 别用 ResizeToContents 模式, 每 setItem 一次就重算整列, 300 行搜索卡一分钟. 填完调 _autosize 一次"""
    hh = tbl.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.Interactive)
    hh.setStretchLastSection(True)
    tbl.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    tbl.setWordWrap(False)


def _autosize(tbl):
    tbl.resizeColumnsToContents()
    for c in range(tbl.columnCount() - 1):
        tbl.setColumnWidth(c, min(tbl.columnWidth(c) + 12, 420))


def _card_spin(value=0, what="card"):
    # 名字搜索框, 不再是数字框
    from mdstudio.gui.cardpick import CardPicker
    return CardPicker(value, what)


def _btn(text, slot, kind=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)

    def safe():
        # 卡名没选好之类的错误在取参数时就抛了, 还没进 run(), 这里兜住, 不然走 excepthook 弹"崩溃"
        try:
            slot()
        except MDStudioError as e:
            QMessageBox.warning(b.window(), tr("Check the input"), str(e))
    b.clicked.connect(safe)
    return b


def _row(*widgets):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        h.addWidget(x) if not isinstance(x, int) else h.addStretch(x)
    return w


class FilePick(QWidget):
    def __init__(self, caption, filt="", folder=False, allow_folder=False):
        super().__init__()
        self.caption, self.filt, self.folder, self.allow_folder = caption, filt, folder, allow_folder
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        h.addWidget(self.edit)
        h.addWidget(_btn(tr("File…") if not folder else tr("Folder…"), self._pick))
        if allow_folder:
            h.addWidget(_btn(tr("Folder…"), self._pick_dir))

    def _pick(self):
        if self.folder:
            return self._pick_dir()
        p, _ = QFileDialog.getOpenFileName(self, self.caption, "", self.filt)
        if p:
            self.edit.setText(p)

    def _pick_dir(self):
        p = QFileDialog.getExistingDirectory(self, self.caption)
        if p:
            self.edit.setText(p)

    def text(self):
        return self.edit.text().strip()


class Main(MicaWindow):
    def __init__(self, game_dir=None):
        super().__init__("MD Cut-in Studio")
        self.pool = QThreadPool.globalInstance()
        self.ctx = None
        self._busy = 0
        # 坑: Job 跑完 Qt 就把它删了, 连在它信号上的 lambda 排队中的调用跟着被丢 -> _finish 永远不跑,
        # 界面一直锁着. 自己拿着引用, 等槽函数在主线程跑完再放
        self._jobs = set()

        self.bridge = LogBridge()
        self.bridge.sink.line.connect(self._log_line)
        self.bridge.sink.progress.connect(self._on_progress)
        log.addHandler(self.bridge)
        self._unlisten = progress.listen(self.bridge.on_progress)
        self._job_title = ""
        self._job_t0 = self._stage_t0 = 0.0
        self._stage = ""
        self._tick = QTimer(self)          # 每秒刷新"已用时间", 卡在一步很久也看得出它还活着
        self._tick.timeout.connect(self._show_status)
        log.setLevel(logging.INFO)

        body = self.body()
        h = QHBoxLayout(body)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        self.nav = Nav([("cards", tr("Cards")), ("cutin", tr("Cut-in")),
                        ("runfx", tr("Activation effect")),
                        ("pack", tr("Share / install")), ("hist", tr("History")),
                        ("settings", tr("Settings"))],
                       footer=f"V {__version__}")
        h.addWidget(self.nav)

        right = QWidget()
        v = QVBoxLayout(right)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(10)

        self.db = None
        self.stack = QStackedWidget()
        # 五个页面原来挂在 QTabWidget 上, 现在挂在左侧导航上
        self.t_cards = self._tab_cards()
        self.t_cutin = self._tab_cutin()
        self.t_runfx = self._tab_runfx()
        self.t_pack = self._tab_pack()
        self.t_hist = self._tab_history()
        self.t_set = self._tab_settings()
        for page in (self.t_cards, self.t_cutin, self.t_runfx, self.t_pack,
                     self.t_hist, self.t_set):
            self.stack.addWidget(page)

        # 表单行距: QFormLayout 默认挤得很紧, 标题与输入框贴在一起读起来累.
        # 注意要遍历页面自身: 此刻 right 还没 addWidget 到 body, 从 self 往下查是空的
        for page in (self.t_cards, self.t_cutin, self.t_runfx, self.t_pack,
                     self.t_hist, self.t_set):
            for form in page.findChildren(QFormLayout):
                form.setVerticalSpacing(10)
                form.setHorizontalSpacing(14)

        # 日志区固定在底部, 不再用 splitter: 拖拽会把它拉得过高, 而且和页面内容
        # 的左边界差 9px (页面布局自带内边距). 包一层容器把这段补回来
        self.console = QPlainTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        _mono = QFont()
        _mono.setFamilies([theme.FONT_MONO, "Consolas"])   # 前者是 Win11 自带, 后者兜底
        _mono.setPointSizeF(9.5)
        self.console.setFont(_mono)
        self.console.setMaximumBlockCount(5000)
        self.console.setFixedHeight(82)          # 大约 4 行, 不抢正文
        self.console.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        v.addWidget(self.stack, 1)
        log_box = QWidget()
        lb = QVBoxLayout(log_box)
        lb.setContentsMargins(9, 0, 9, 0)        # 与页面内容左右对齐
        lb.addWidget(self.console)
        v.addWidget(log_box)

        # 状态栏压在底部一行里: 状态 + 用时 + 一条细进度. 拆成两行太占地方
        self.status = QLabel(tr("Ready"))
        self.status.setObjectName("Status")
        self.status_time = QLabel("")
        self.status_time.setObjectName("Muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedSize(120, 4)
        v.addWidget(_row(self.status, 1, self.status_time, self.progress))
        h.addWidget(right, 1)

        self.nav.changed.connect(self._page_changed)
        self.dry_badge_clicked.connect(lambda: self.nav.set_current(5))
        self.nav.set_current(0)

        self._open_game(game_dir)

    def _cut_mode_changed(self, i):
        """两种导入方式互斥, 只显示选中的那个面板; 主按钮跟着换"""
        if hasattr(self, "panel_video"):
            self.panel_video.setVisible(i == 0)
            self.panel_spine.setVisible(i == 1)
            self.cv_build.setVisible(i == 0)
            self.mp_build.setVisible(i == 1)

    def _page_changed(self, row):
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        self.refresh()

    def on_theme_changed(self, t):
        """主题切换后重画导航图标 (QIcon 的选中态颜色是烘焙进去的)"""
        if getattr(self, "nav", None) is not None:
            self.nav.apply_theme(t)

    # ------------------------------------------------------------------ header
    def _sync_title(self):
        """账号身份挂在标题栏上, 不占正文那行"""
        acc = self.ctx.game.group if self.ctx else ""
        self.set_account_label(settings.label(acc) if acc else "")
        if self.ctx:
            self.setWindowTitle(f"MD Cut-in Studio — {acc}")
        else:
            self.setWindowTitle("MD Cut-in Studio")

    def _open_game(self, game_dir=None, account=None):
        """定位游戏并切到指定账号

        game_dir 为空就用设置里存的目录, 再没有就自动检测; account 为空则用设置里的,
        仍然无效就退回"文件最多"的兜底 (只在不认识这台机器时发生).
        """
        try:
            saved = settings.load()
            gdir = game_dir or saved.get("game_dir") or None
            want = account if account is not None else saved.get("account")
            gdir = gdir or detect_game_dir()
            group = want if want and settings.valid_account(gdir, want) else None
            self.ctx = Ctx(Game(gdir, group))
            settings.save(game_dir=str(self.ctx.game.dir), account=self.ctx.game.group)
            self._sync_settings_page()
            self._load_names()
            self.refresh()
        except MDStudioError as e:
            self.ctx = None
            self._sync_settings_page()
            log.error(str(e))

    def _choose_game(self):
        p = QFileDialog.getExistingDirectory(self, tr("Master Duel folder (the one with LocalData)"))
        if p:
            if not settings.valid_account(p, ""):
                QMessageBox.warning(self, tr("Not a game folder"),
                                    tr("%s does not contain LocalData/<account>/0000.\n"
                                       "Pick the folder that holds LocalData.") % p)
                return
            self._open_game(p, account="")      # 换目录后旧账号无效, 让它重新枚举

    def on_auto_detect(self):
        try:
            p = detect_game_dir()
        except MDStudioError as e:
            QMessageBox.warning(self, tr("Not found"), str(e))
            return
        self._open_game(p, account="")

    def on_account_pick(self, i):
        if i < 0 or not self.ctx:
            return
        acc = self.set_account.itemData(i)
        if acc and acc != self.ctx.game.group:
            log.info(tr("switched to account %s"), acc)
            self._open_game(str(self.ctx.game.dir), account=acc)

    def on_note_name(self):
        """备注名一改就落盘并刷新标题栏, 不需要额外的保存按钮"""
        if not self.ctx:
            return
        settings.set_account_name(self.ctx.game.group, self.set_note.text())
        self._sync_title()

    def _on_dry_toggle(self, on):
        self.set_dry_run(bool(on))

    def _sync_settings_page(self):
        """把当前状态回填到设置页 (没有 ctx 时也要显示得出来)"""
        if not hasattr(self, "set_dir"):
            return
        gdir = str(self.ctx.game.dir) if self.ctx else settings.get("game_dir", "")
        self.set_dir.setText(gdir or tr("(not found)"))
        self.set_dir.setCursorPosition(0)      # 长路径默认显示尾部, 拉回开头
        self.set_dir.setToolTip(gdir or "")      # 长路径默认显示尾部, 拉回开头
        self.set_account.blockSignals(True)
        self.set_account.clear()
        accs = list_accounts(gdir) if gdir else []
        cur = self.ctx.game.group if self.ctx else ""
        for a in accs:
            self.set_account.addItem(tr("%s  ·  %s files") % (a, _file_count(gdir, a)), a)
        if accs:
            idx = accs.index(cur) if cur in accs else 0
            self.set_account.setCurrentIndex(idx)
        else:
            self.set_account.addItem(tr("(no account found)"), "")
        self.set_account.blockSignals(False)
        self.set_journal.setText(str(self.ctx.journal.folder) if self.ctx
                                 else tr("(none yet)"))
        if hasattr(self, "set_note"):
            self.set_note.blockSignals(True)
            self.set_note.setText(settings.account_name(cur) if cur else "")
            self.set_note.setEnabled(bool(cur))
            self.set_note.blockSignals(False)
        self._sync_title()
        if self.ctx and hasattr(self, "hist_note"):
            self.hist_note.setText(tr("Journal + backups: %s") % self.ctx.journal.folder)

    # ------------------------------------------------------------------ jobs
    def need_game(self) -> bool:
        if not self.ctx:
            QMessageBox.warning(self, tr("No game"),
                                tr("Pick the game folder first (Settings → Game folder)."))
            return False
        return True

    def _start(self, job):
        self._jobs.add(job)
        self.pool.start(job)

    def run(self, fn, *args, done=None, **kw):
        if not self.need_game():
            return
        if self._busy:
            QMessageBox.information(self, tr("Busy"),
                                    tr("Still working on: %s. Wait for it to finish.") % self._job_title)
            return
        # 每个任务自己的 Ctx: 跑的途中勾/取消 Dry run 不能影响正在写的那个
        ctx = Ctx(self.ctx.game, dry_run=self.dry.isChecked(), journal=self.ctx.journal,
                  check_running=self.ctx.check_running)
        self._busy += 1
        self._job_title = _job_title(fn) + (tr(" (dry run)") if ctx.dry_run else "")
        self._job_t0 = self._stage_t0 = time.monotonic()
        self._stage = tr("starting…")
        log.info("▶ %s", self._job_title)
        self.progress.setRange(0, 0)        # 先转圈, op 报了 i/n 再变成真进度
        self.progress.setTextVisible(False)
        self._tick.start(1000)
        self._show_status()
        self._lock_ui(True)
        if _job_key(fn) in HEAVY and picklable(fn):
            job = ProcJob(fn, ctx, *args, **kw)     # 压贴图不放 GIL, 放子进程, 窗口才不卡
            job.sink = self.bridge.sink
        else:
            job = Job(fn, ctx, *args, **kw)
        job.signals.on_done = done
        job.signals.done.connect(self._finish)
        job.signals.failed.connect(self._failed)
        job.signals.done.connect(lambda _r, j=job: self._jobs.discard(j))
        job.signals.failed.connect(lambda _m, j=job: self._jobs.discard(j))
        self._start(job)

    def _lock_ui(self, on):
        for w in (self.nav, self.stack, self.dry):
            w.setEnabled(not on)

    def _on_progress(self, text, i, n):
        if not self._busy:          # 后台读卡名之类的, 不占状态栏
            return
        if text != self._stage:
            self._stage, self._stage_t0 = text, time.monotonic()
        if i < 0 or n <= 0:
            self.progress.setRange(0, 0)
            self.progress.setTextVisible(False)
        else:
            self.progress.setRange(0, n)
            self.progress.setValue(min(i, n))
            self.progress.setFormat(f"{i}/{n}  (%p%)")
            self.progress.setTextVisible(True)
        self._show_status()

    def _show_status(self):
        if not self._busy:
            return
        now = time.monotonic()
        txt = tr("⏳ %s — %s") % (self._job_title, self._stage)
        extra = tr("elapsed %s") % _clock(now - self._job_t0)
        i, n = self.progress.value(), self.progress.maximum()
        if n > 0 and 0 < i < n:      # 按这一步目前的速度估剩余时间
            left = (now - self._stage_t0) / i * (n - i)
            if left >= 2:
                extra = tr("this step ~%s left  ·  ") % _clock(left) + extra
        self.status.setText(txt)
        self.status_time.setText(extra)

    def _end_status(self, ok):
        self._tick.stop()
        took = _clock(time.monotonic() - self._job_t0)
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if ok else 0)
        self.progress.setTextVisible(False)
        self.status.setText(f"{'✔' if ok else '✖'} {self._job_title} — {tr('done') if ok else tr('failed')}")
        self.status_time.setText(tr("took %s") % took)
        if ok:
            log.info(tr("✔ %s: done in %s"), self._job_title, took)
        else:
            log.error(tr("✖ %s: failed after %s"), self._job_title, took)

    def _finish(self, result):
        done = getattr(self.sender(), "on_done", None)
        self._end_status(True)
        self._settle()
        try:
            if done:
                done(result)
        finally:
            self._after_change()

    def _failed(self, msg):
        self._end_status(False)
        self._settle()
        log.error(tr("error: %s"), msg)
        QMessageBox.warning(self, tr("Didn't work"), msg)
        self._after_change()

    def _after_change(self):
        # 改完东西 donor 列表也可能变了 (自己加的也能当 donor)
        if hasattr(self, "fx_donors"):
            self.fx_donors.setRowCount(0)
        self._inv = None
        self._fill_pickers()
        self.refresh()

    def _settle(self):
        self._busy = max(0, self._busy - 1)
        if not self._busy:
            self._lock_ui(False)

    def closeEvent(self, ev):
        if self._busy and QMessageBox.question(
                self, tr("Still working"),
                tr("A job is still writing game files. Closing now can leave it half done.\nClose anyway?")) \
                != QMessageBox.Yes:
            ev.ignore()
            return
        self._unlisten()
        log.removeHandler(self.bridge)
        super().closeEvent(ev)

    def _log_line(self, text, level):
        color = "#e05050" if level >= logging.ERROR else "#e0a030" if level >= logging.WARNING else "#c8c8c8"
        stamp = time.strftime("%H:%M:%S")
        self.console.appendHtml(f"<span style='color:#5c636d'>{stamp}</span> "
                                f"<span style='color:{color}'>{text.replace('<', '&lt;')}</span>")

    def _confirm(self, text):
        return QMessageBox.question(self, tr("Confirm"), text) == QMessageBox.Yes

    # ------------------------------------------------------------------ cards
    def _load_names(self):
        # 卡名第一次要扫 bundle (更新后十几秒), 放后台, 不挡 UI
        from mdstudio.core.cards import CardDB
        self.db = None
        self.card_status.setText(tr("loading card names…"))
        job = Job(lambda: CardDB(self.ctx.game).load())
        job.signals.done.connect(self._names_ready)
        job.signals.failed.connect(self._names_failed)
        job.signals.done.connect(lambda _r, j=job: self._jobs.discard(j))
        job.signals.failed.connect(lambda _m, j=job: self._jobs.discard(j))
        self._start(job)

    def _names_failed(self, msg):
        self.card_status.setText(tr("card names unavailable: %s") % msg)
        log.warning(tr("card names unavailable: %s (type card IDs instead)"), msg)

    def _names_ready(self, db):
        self.db = db
        self.card_status.setText(tr("%s cards") % len(db.names))
        self._fill_pickers()
        self._card_search()
        self.refresh()

    def _fill_pickers(self):
        """卡名到了之后给所有卡输入框装下拉. donor 只列真有动画的, 免得选了不能用的"""
        if not self.db or not self.ctx:
            return
        from PySide6.QtWidgets import QCompleter
        from mdstudio.ops import runfx as R
        from mdstudio.ops import sound as S
        inv = self._inventory()
        self.cut_card.set_names(self.db)
        self.fx_card.set_names(self.db)
        self.cut_donor.set_names(self.db, [c for c, st in inv.cutin.items() if st in ("game", "yours")])
        self.fx_donor.set_names(self.db, [c for c, st in inv.runfx.items() if st in ("game", "yours")])
        labels = sorted({se for _m, _t, se, _ep, _ok in R.list_effects(self.ctx.game)
                         if se and S.on_disk(self.ctx.game, se)})
        for box in (self.fv_sound, self.fs_label):
            cur = box.currentText()
            box.clear()
            box.addItems([""] + labels)
            if box is self.fv_sound and not cur:
                cur = default_carrier(labels)     # 视频声音默认借这个, 不让人空着
            box.setCurrentText(cur)
            comp = box.completer()
            comp.setFilterMode(Qt.MatchContains)
            comp.setCompletionMode(QCompleter.PopupCompletion)
        self._auto_sound()        # 特效刚改过 (或卡名刚到), 音效那栏跟着更新

    def _tab_cards(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.card_q = QLineEdit()
        self.card_q.setPlaceholderText(tr("Search a card by name or ID…"))
        self.card_q.setMinimumWidth(300)
        self.card_q.setMaximumWidth(340)     # 别让它吃满整行, 否则右边那串状态被挤到边上
        # 打字时别每个键都查, 停 150ms 再查
        self._search_timer = QTimer(self, singleShot=True, interval=150)
        self._search_timer.timeout.connect(self._card_search)
        self.card_q.textChanged.connect(self._search_timer.start)
        self.card_has = QComboBox()
        for label, key in ((tr("All cards"), None), (tr("Has a cut-in"), "cutin"),
                           (tr("Has an activation effect"), "runfx"),
                           (tr("Has either"), "any"), (tr("Has neither"), "none")):
            self.card_has.addItem(label, key)
        self.card_has.currentIndexChanged.connect(self._card_search)
        self.card_status = QLabel("")
        self.card_status.setObjectName("Muted")
        # 末尾给弹簧: 否则富余宽度会被平均分给下拉框和状态标签, 状态被推到最右边看不清
        v.addWidget(_row(self.card_q, self.card_has, self.card_status, 1))

        # 五列: ID 与三个状态列都是定宽, 只有名称可拉伸 (名字长短差别大)
        self.card_tbl = QTableWidget(0, 5)
        self.card_tbl.setHorizontalHeaderLabels(["ID", tr("Name"), tr("Cut-in"), tr("Effect"),
                                                 tr("Same name, other ID")])
        _fit_columns(self.card_tbl)
        self.card_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.card_tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.card_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.card_tbl.verticalHeader().hide()
        # 状态列画成标签
        for c in (2, 3):
            self.card_tbl.setItemDelegateForColumn(c, pill.PillDelegate(self.card_tbl))
        hh = self.card_tbl.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)      # 只有名称列吃富余宽度
        for col, cw in ((0, 68), (2, 132), (3, 132), (4, 176)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.card_tbl.setColumnWidth(col, cw)
        hh.setDefaultAlignment(Qt.AlignCenter)               # 表头居中, 与定宽列的组合更整齐
        # 竖滚动条常显: 它一直占着右侧那 14px, 藏起来就像表格右边留了一块空白
        self.card_tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.card_tbl.itemSelectionChanged.connect(self._card_detail)
        v.addWidget(self.card_tbl, 1)

        self.card_info = QLabel("")
        self.card_info.setWordWrap(True)
        self.card_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.card_info.hide()        # 没选中卡时不占位置, 否则表格与按钮之间会多出一条空隙
        v.addWidget(self.card_info)
        v.addWidget(_row(
            _btn(tr("→ Cut-in: this card"), lambda: self._send(self.cut_card, self.t_cutin)),
            _btn(tr("→ Cut-in: use as donor"), lambda: self._send(self.cut_donor, self.t_cutin)),
            _btn(tr("→ Effect: this card"), lambda: self._send(self.fx_card, self.t_runfx)),
            _btn(tr("→ Effect: use as donor"), lambda: self._send(self.fx_donor, self.t_runfx)), 1))
        return w

    def _inventory(self):
        # 读 gate 要几十 ms, 打字时复用. refresh() 会清掉
        from mdstudio.ops.inventory import Inventory
        if getattr(self, "_inv", None) is None or self._inv.db is not self.db:
            self._inv = Inventory(self.ctx.game, self.ctx.journal, self.db)
        return self._inv

    def _card_search(self, *_):
        if not self.db or not self.ctx:
            return
        q = self.card_q.text().strip()
        inv = self._inventory()
        has = self.card_has.currentData()
        if q:
            rows = inv.search(q, has=has, limit=300)
        elif has in ("cutin", "runfx"):
            rows = inv.cards_with(has)
        elif has == "any":
            rows = [inv.info(c) for c in sorted(set(inv.cutin) | set(inv.runfx))]
        else:
            rows = []
        self._card_rows = rows
        self.card_tbl.setUpdatesEnabled(False)
        self.card_tbl.setRowCount(len(rows))
        bold = QFont()
        bold.setWeight(QFont.DemiBold)
        muted = QColor(theme.qcolor(theme.tokens().muted))
        for r, ci in enumerate(rows):
            other = []
            if ci.sibling_cutin:
                other.append(tr("cut-in on %s") % ', '.join(map(str, ci.sibling_cutin)))
            if ci.sibling_runfx:
                other.append(tr("effect on %s") % ', '.join(map(str, ci.sibling_runfx)))
            # 没有状态就留空, 画个 "-" 的标签反而碍眼
            vals = (ci.id, ci.name,
                    STATUS_TEXT[ci.cutin] if ci.cutin else "",
                    STATUS_TEXT[ci.runfx] if ci.runfx else "",
                    "; ".join(other))
            for c, val in enumerate(vals):
                it = _cell(val)
                if c == 0:
                    it.setForeground(muted)
                elif c == 1:
                    it.setFont(bold)                       # 名字是关键信息, 加重
                elif c in (2, 3):
                    st = ci.cutin if c == 2 else ci.runfx
                    if st:
                        # 官方自带用红色, 与召唤动画页的「官方有动画」标签保持一致
                        it.setData(pill.ROLE_COLOR, STATUS_COLOR["official" if st == "game" else st])
                elif c == 4:
                    it.setTextAlignment(Qt.AlignCenter)    # 定宽列居中, 跟表头一致
                self.card_tbl.setItem(r, c, it)
        self.card_tbl.setUpdatesEnabled(True)
        if q or has not in (None, "none"):
            self.card_status.setText(tr("%s result(s)") % len(rows))
        else:
            self.card_status.setText(tr("%s cards") % len(self.db.names))

    def _card_detail(self):
        r = self.card_tbl.currentRow()
        if r < 0 or r >= len(getattr(self, "_card_rows", [])):
            self.card_info.hide()        # 没选中就不占高度
            return
        ci = self._card_rows[r]
        lines = [f"<b>{ci.name}</b>  ({ci.id})"]
        for t, se, ep in ci.effects:
            lines.append(tr("effect: %s · sound %s · %s")
                         % (tr(t), se or '-', ep.rsplit('/', 1)[-1] if ep else tr("built-in")))
        if not ci.cutin and ci.sibling_cutin:
            lines.append(tr("This printing has no cut-in, but %s (same card) does. "
                            "If you play that version you already have it.")
                         % ', '.join(map(str, ci.sibling_cutin)))
        self.card_info.setText("<br>".join(lines))
        self.card_info.setVisible(True)

    def _send(self, spin, tab):
        r = self.card_tbl.currentRow()
        if r < 0 or r >= len(getattr(self, "_card_rows", [])):
            QMessageBox.information(self, tr("Pick a card"), tr("Click a card in the table first."))
            return
        spin.setValue(self._card_rows[r].id)
        i = self.stack.indexOf(tab)      # 走导航, 让左侧高亮跟页面一起切
        if i >= 0:
            self.nav.set_current(i)
            self.stack.setCurrentIndex(i)

    # ------------------------------------------------------------------ cut-in
    def _tab_cutin(self):
        from mdstudio.ops import cutin as C
        w = QWidget()
        h = QHBoxLayout(w)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)      # 页面布局已有 9px, 这里再加两栏会错位
        left.setSpacing(10)
        self.cut_list = QListWidget()
        self.cut_list.itemClicked.connect(lambda it: self.cut_card.setValue(it.data(Qt.UserRole)))
        left.addWidget(_list_card(tr("Your cut-ins"), self.cut_list))
        h.addLayout(left, 1)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        g = QGroupBox(tr("Give a card a cut-in"))
        f = QFormLayout(g)
        self.cut_card = _card_spin()
        self.cut_donor = _card_spin(C.DEFAULT_DONOR, "donor")
        # 按召唤类型挑好的源卡: 借用会把源卡整套资产搬过来, 类型对不上时前面会有 1 秒空转
        self.cut_donor_preset = QComboBox()
        self.cut_donor_preset.addItem(tr("(custom)"), 0)
        for label, cid in DONOR_PRESETS:
            self.cut_donor_preset.addItem(tr(label), cid)
        self.cut_donor_preset.setToolTip(tr("Pick a donor whose summon type matches the card you're editing. "
                                            "All of these were test-cloned successfully."))
        self.cut_donor_preset.currentIndexChanged.connect(self.on_donor_preset)
        # activated 在"选了当前项"时也发. 只连 currentIndexChanged 的话,
        # 把来源卡清空后再选同一个预设不会回填, 因为索引没变
        self.cut_donor_preset.activated.connect(self.on_donor_preset)
        # 目标卡官方自带召唤动画时给出提示, 并把来源卡默认填成它自己
        self._auto_donor = None          # 记住来源卡是不是我们自动填的
        self.cut_official = pill.Pill(tr("Has a game cut-in"), STATUS_COLOR["official"])
        self.cut_official.hide()
        f.addRow(tr("Card ID"), _row(self.cut_card, self.cut_official))
        f.addRow(tr("Donor card ID"), _row(self.cut_donor, self.cut_donor_preset))
        self.cut_card.textChanged.connect(self._sync_target_hint)
        # 用户自己改过来源卡, 就不算我们自动填的了
        self.cut_donor.textEdited.connect(lambda _t: setattr(self, "_auto_donor", None))
        # 只留克隆: 借用会让两张卡共用同一套资产, 改一张影响另一张, 不如直接复制一份
        # 按钮与说明同一行: 说明占剩下的宽度, 只留一句, 全文进悬停提示
        self.cut_hint = QLabel()
        self.cut_hint.setObjectName("Muted")
        clone_btn = _btn(tr("Clone (editable copy)"), self.on_cut_clone, "Primary")
        clone_btn.setToolTip(tr("Clone copies the donor's whole cut-in onto your card. The copy is "
                                "yours: reskin and video rebuild work on it, the donor is untouched."))
        self.cut_btn = clone_btn
        f.addRow(_row(clone_btn, _btn(tr("Remove"), self.on_cut_remove, "Danger"),
                      self.cut_hint, 1))
        right.addWidget(g)

        # 两种导入方式互斥: 一次只显示一个面板, 主按钮跟着自己的面板走.
        # 之前两张卡片并排堆着, 「生成召唤动画」落在视频那张卡里, 看着像页面级按钮
        self.cut_mode = segmented.Segmented([tr("Video / GIF / frames"), tr("Spine")])
        self.cut_mode.changed.connect(self._cut_mode_changed)
        # 主操作跟着切换栏走: 常驻可见, 不用滚到面板底部去找
        self.cv_build = _btn(tr("Build cut-in"), self.on_cut_video, "Primary")
        self.mp_build = _btn(tr("Import to this card"), self.on_spine_card, "Primary")
        self.mp_build.hide()
        right.addWidget(_row(self.cut_mode, 1, self.mp_build, self.cv_build))

        # 用显隐而不是 QStackedWidget: 堆叠容器的高度取所有页的最大值,
        # 切到 Spine 会留一大片空白, 还能滚进空处
        panel_v = QWidget()
        panel_v.setObjectName("Card")
        pv = QVBoxLayout(panel_v)
        pv.setContentsMargins(12, 12, 12, 12)
        panel_s = QWidget()
        panel_s.setObjectName("Card")
        ps = QVBoxLayout(panel_s)
        ps.setContentsMargins(12, 12, 12, 12)

        g = QWidget()
        f = QFormLayout(g)
        f.setContentsMargins(0, 0, 0, 0)
        self.cv_src = FilePick(tr("Video"), tr("Video (*.mp4 *.webm *.mov *.gif);;All (*)"), allow_folder=True)
        # 方案: 一次带出下面这一整排参数 (模式/帧数/像素/时长/尺寸/偏移/旋转/倍率/曲线/锐化)
        self.cv_scheme = QComboBox()
        self.cv_scheme.setMinimumWidth(140)
        self.cv_scheme.currentIndexChanged.connect(self.on_scheme_pick)
        # 已经停在某一项时再选一次不会发 currentIndexChanged, activated 才发,
        # 所以两个都接上: 改别的参数后重新点同一个方案也能一键还原
        self.cv_scheme.activated.connect(self.on_scheme_pick)
        self.cv_scheme_save = _btn(tr("Save as scheme…"), self.on_scheme_save)
        self.cv_scheme_del = _btn("×", self.on_scheme_delete)
        self.cv_scheme_del.setToolTip(tr("Delete the selected scheme"))
        self._schemes = SCH.load()
        self._refresh_schemes()
        # 默认整段原样全屏 (Phainon 就是这么做的); 抠图 (rembg) 只在自己选的时候跑
        self.cv_frames = QSpinBox()
        self.cv_frames.setRange(4, 200)
        self.cv_frames.setValue(120)
        self.cv_cell = QSpinBox()
        self.cv_cell.setRange(128, 2048)
        self.cv_cell.setValue(1080)
        self.cv_dur = QDoubleSpinBox()
        self.cv_dur.setRange(0.4, 15.0)
        self.cv_dur.setValue(4.8)
        self.cv_size = QSpinBox()
        self.cv_size.setRange(500, 8000)
        self.cv_size.setValue(4800)
        self.cv_size.setToolTip(tr("4800 = fills a 16:9 screen. ~2750 = the size of Konami's cut-ins"))
        # 整体位移 (单位是帧图像素, 正向: X 向右 / Y 向上) 和整体倍率
        self.cv_offx = QSpinBox()
        self.cv_offx.setRange(-3000, 3000)
        self.cv_offx.setSuffix(" px")
        self.cv_offx.setValue(0)
        self.cv_offx.setToolTip(tr("Shift the whole cut-in sideways; positive = right. "
                                   "Unit = pixels of the frame image."))
        self.cv_offy = QSpinBox()
        self.cv_offy.setRange(-3000, 3000)
        self.cv_offy.setSuffix(" px")
        self.cv_offy.setValue(0)
        self.cv_offy.setToolTip(tr("Shift the whole cut-in up/down; positive = up. "
                                   "Unit = pixels of the frame image."))
        self.cv_rot = QSpinBox()
        self.cv_rot.setRange(-180, 180)
        self.cv_rot.setSuffix("°")
        self.cv_rot.setValue(0)
        self.cv_rot.setToolTip(tr("Rotate the whole cut-in; positive = counter-clockwise (degrees)."))
        self.cv_zoom = QDoubleSpinBox()
        self.cv_zoom.setRange(0.2, 3.0)
        self.cv_zoom.setSingleStep(0.05)
        self.cv_zoom.setDecimals(2)
        self.cv_zoom.setValue(1.0)
        self.cv_zoom.setToolTip(tr("Overall zoom multiplied onto the pop curve. 1 = as generated."))
        self.cv_pop = QCheckBox(tr("Pop curve"))
        self.cv_pop.setChecked(False)
        self.cv_pop.toggled.connect(self._sync_curve_ui)
        # 弹出曲线预设: 参数不对外展示, 用户只看到名字. 选「自定义」才出现输入框
        self.cv_preset = QComboBox()
        self.cv_preset.addItem(tr("Silvicola preset"), POP_CURVE_SILVICOLA)
        self.cv_preset.addItem(tr("Custom"), None)
        self.cv_preset.currentIndexChanged.connect(self._curve_preset_changed)
        # 自定义时的起点 (也是默认预设的参数, 方便在它上面微调)
        self.cv_curve = QLineEdit(POP_CURVE_SILVICOLA)
        self.cv_curve.setToolTip(tr("One group per keyframe: frame,scale,opacity in percent, "
                                    "separated by <. Example: 0,10,10<8,70,50. Blank = the built-in curve."))
        self.cv_sharp = QCheckBox(tr("Sharpen (Real-ESRGAN)"))
        from mdstudio.ops.video.frames import sharpen_available
        self.cv_sharp.setEnabled(sharpen_available())
        # 以前是内置方案带出「勾上」, 现在方案不管锐化了, 这里直接默认勾 (GPU 可用时)
        self.cv_sharp.setChecked(sharpen_available())
        self.cv_sharp.setToolTip(tr("4x super-resolution on the GPU. The fast model takes about "
                                    "2s per frame, the slow one about 7s. "
                                    "Needs tools/realesrgan/realesrgan-ncnn-vulkan.exe."))
        # 两个模型: videov3 快且抗视频压缩噪点; x4plus-anime 慢但线条更漂亮
        self.cv_sharp_model = QComboBox()
        self.cv_sharp_model.addItem(tr("Fast — video footage (≈2s/frame)"), "realesr-animevideov3")
        self.cv_sharp_model.addItem(tr("Slow — anime line art (≈7s/frame, cleaner)"),
                                    "realesrgan-x4plus-anime")
        self.cv_sharp_model.setEnabled(False)
        self.cv_sharp_model.setToolTip(tr("Fast: trained on compressed video, keeps up with noisy "
                                          "footage, about 2 seconds per frame. Slow: trained on anime "
                                          "line art, cleaner edges, about 7 seconds per frame "
                                          "(3x the time for the same clip)."))
        # 不勾锐化就把模型下拉收起来, 只留一个勾选框
        self.cv_sharp.toggled.connect(self.on_sharp_toggled)
        self.on_sharp_toggled(self.cv_sharp.isChecked())
        f.addRow(tr("Source"), self.cv_src)
        # 锐化在方案上方: 它不跟着方案走, 影响的是"怎么处理"而不是画面摆位
        f.addRow(tr("Sharpening"), _row(self.cv_sharp, self.cv_sharp_model, 1))
        f.addRow(tr("Scheme"), _row(self.cv_scheme, self.cv_scheme_save, self.cv_scheme_del, 1))
        # 一行塞四个数字框会撑到 740 宽, 加了左侧导航以后放不下, 拆成两行
        # 数值框限宽: 不限的话它们会撑到 105-112, 四对并一行要 599px 而可用只有 583.
        # 偏移那两行标签长 (横向偏移 X), 实测要 674px, 仍然只能两对一行
        for sb in (self.cv_frames, self.cv_cell, self.cv_dur, self.cv_size,
                   self.cv_offx, self.cv_offy, self.cv_rot, self.cv_zoom):
            sb.setMaximumWidth(90)
        f.addRow(_row(QLabel(tr("Frames")), self.cv_frames, QLabel(tr("px/frame")), self.cv_cell,
                      QLabel(tr("Seconds")), self.cv_dur, QLabel(tr("Size")), self.cv_size, 1))
        f.addRow(_row(QLabel(tr("Offset X")), self.cv_offx, QLabel(tr("Offset Y")), self.cv_offy,
                      QLabel(tr("Rotation")), self.cv_rot, QLabel(tr("Zoom")), self.cv_zoom, 1))
        # 勾上才有下面的曲线配置; 预设与自定义输入同一行, 预设短一点别占满整行
        # 勾选框当行标签用, 曲线下拉同一行: 少一行, 也省掉「曲线」这个多余的标签
        # 预设框跟着字段列伸缩: 只显示预设时铺满, 出现自定义框时两者对半
        self.cv_preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cv_preset.setMaximumWidth(240)
        # 勾选框与下拉都放字段列: 两者只隔一个标准间距, 左缘也跟其它字段对齐.
        # 之前把勾选框放标签列, 标签列比内容宽, 中间空出 148px
        f.addRow("", _row(self.cv_pop, self.cv_preset, self.cv_curve, 1))
        self._cv_form = f
        self._curve_preset_changed()
        # 启动时把方案一的画面参数填进来, 否则下拉显示「方案一」但数字还是构造默认值
        self.on_scheme_pick()
        pv.addWidget(g)
        pv.addStretch(1)

        g = QWidget()
        f = QFormLayout(g)
        f.setContentsMargins(0, 0, 0, 0)
        self.mp_src = FilePick(tr("Spine source"), "Spine / bundle / zip (*)", allow_folder=True)
        f.addRow(tr("Source"), self.mp_src)
        f.addRow(_row(_btn(tr("Import all"), self.on_spine_all),
                      _btn(tr("Check installed"), self.on_spine_check), 1))
        hint = QLabel(tr("Source: a Spine 4.x export folder, zip or bundle (goes on the Card above), or a "
                         "folder of them named by passcode / listed in cards.json (Import all skips cards "
                         "that have a cut-in)."))
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        f.addRow(hint)
        ps.addWidget(g)
        ps.addStretch(1)

        right.addWidget(panel_v)
        right.addWidget(panel_s)
        panel_s.hide()
        self.panel_video, self.panel_spine = panel_v, panel_s

        g = QGroupBox(tr("Reskin (clones only)"))
        f = QVBoxLayout(g)
        f.addWidget(QLabel(tr("Export the texture pages + atlas, paint inside the regions, keep the size, import.")))
        f.addWidget(_row(_btn(tr("Export textures…"), self.on_reskin_export),
                         _btn(tr("Import PNGs…"), self.on_reskin_import), 1))
        # 用得少、门槛高: 收进「高级」, 不占常用路径的地方
        right.addWidget(collapsible.Collapsible(tr("Advanced"), g))
        right.addStretch(1)
        area = _scroll(right)
        area.setMinimumWidth(560)        # 表单再窄就会被右边裁掉, 让表格那侧让步
        h.addWidget(area, 2)
        return w

    def on_cut_remove(self):
        from mdstudio.ops import cutin as C
        c = self.cut_card.value()
        if not C._ours(self.ctx, c):
            # 没有可回退的东西: 说清楚现状, 别让人点出一个 op 报错
            if self._is_official(c):
                QMessageBox.information(
                    self, tr("Nothing to undo"),
                    tr("%s is still the game's own cut-in. There's nothing of ours to remove.") % c)
            else:
                QMessageBox.information(
                    self, tr("Nothing to undo"),
                    tr("%s has no cut-in from this tool yet.") % c)
            return
        if self._confirm(tr("Remove the cut-in this tool added to %s?") % c):
            self.run(C.remove, c)

    def on_sharp_toggled(self, on: bool):
        """不勾锐化就藏掉模型下拉 (以前只是变灰, 看着像坏掉)"""
        self.cv_sharp_model.setVisible(bool(on))
        self.cv_sharp_model.setEnabled(bool(on))

    def _sync_curve_ui(self):
        """勾选弹出曲线才露配置; 只有选「自定义」时才显示参数输入框"""
        if not hasattr(self, "_cv_form"):
            return
        on = self.cv_pop.isChecked()
        custom = self.cv_preset.currentData() is None
        self.cv_preset.setVisible(on)
        self.cv_curve.setVisible(on and custom)

    def _curve_preset_changed(self):
        self._sync_curve_ui()

    def _refresh_schemes(self, select=None):
        """重建下拉. blockSignals 是为了别在刷新时把整排参数刷一遍"""
        cur = self.cv_scheme.currentIndex() if select is None else select
        self.cv_scheme.blockSignals(True)
        self.cv_scheme.clear()
        for s in self._schemes:
            self.cv_scheme.addItem(s["name"], s)
        self.cv_scheme.setCurrentIndex(min(max(cur, 0), len(self._schemes) - 1))
        self.cv_scheme.blockSignals(False)
        self.cv_scheme_del.setEnabled(self.cv_scheme.currentIndex() > 0)

    def on_scheme_pick(self):
        """选中方案 -> 只填画面参数 (模式/锐化不归方案管)"""
        s = self.cv_scheme.currentData()
        if not s:
            return
        self.cv_frames.setValue(int(s["frames"]))
        self.cv_cell.setValue(int(s["cell"]))
        self.cv_dur.setValue(float(s["dur"]))
        self.cv_size.setValue(int(s["size"]))
        self.cv_offx.setValue(int(s["offx"]))
        self.cv_offy.setValue(int(s["offy"]))
        self.cv_rot.setValue(int(s["rot"]))
        self.cv_zoom.setValue(float(s["zoom"]))
        p = int(s["preset"])
        if 0 <= p < self.cv_preset.count():
            self.cv_preset.setCurrentIndex(p)
        self.cv_curve.setText(s.get("curve") or self.cv_curve.text())
        self.cv_pop.setChecked(bool(s["pop"]))
        self.cv_scheme_del.setEnabled(self.cv_scheme.currentIndex() > 0)

    def on_scheme_save(self):
        name, ok = QInputDialog.getText(self, tr("Save scheme"), tr("Name of the new scheme:"),
                                        text=self.cv_scheme.currentText())
        name = (name or "").strip()
        if not ok or not name:
            return
        self._schemes.append({
            "name": name,
            "frames": self.cv_frames.value(), "cell": self.cv_cell.value(),
            "dur": round(self.cv_dur.value(), 3), "size": self.cv_size.value(),
            "offx": self.cv_offx.value(), "offy": self.cv_offy.value(),
            "rot": self.cv_rot.value(), "zoom": round(self.cv_zoom.value(), 3),
            "preset": self.cv_preset.currentIndex(), "curve": self.cv_curve.text().strip(),
            "pop": self.cv_pop.isChecked(),
        })
        SCH.save_custom(self._schemes[1:])
        self._refresh_schemes(select=len(self._schemes) - 1)

    def on_scheme_delete(self):
        i = self.cv_scheme.currentIndex()
        if i <= 0:                  # 内置方案一不给删
            return
        if not self._confirm(tr("Delete the scheme \"%s\"?") % self._schemes[i]["name"]):
            return
        self._schemes.pop(i)
        SCH.save_custom(self._schemes[1:])
        self._refresh_schemes(select=max(0, i - 1))

    def _cut_force(self) -> bool:
        """目标卡是否官方自带召唤动画 (只用于标签提示, 不决定写不写)

        勾选框去掉了: 这个判断不需要用户操心, 看目标卡是谁就知道.
        来源卡默认填成目标卡自己 (把官方那套复制一份来改), 用户也可以换别的来源卡。
        """
        target = self.cut_card.peek()
        return bool(target and target in getattr(self, "_official", set()))

    def _cut_has_ours(self) -> bool:
        """目标卡是否已经有本工具加的召唤动画 (覆盖过一次之后就是这种状态)"""
        target = self.cut_card.peek()
        return bool(target and target in getattr(self, "_mine", set()))

    def _is_official(self, card: int) -> bool:
        """问游戏本身这张卡有没有官方召唤动画

        不用 _official 缓存: 那个集合只在刷新召唤动画页时填, 冷启动或刚切页时是空的,
        判成 False 就会把 force 丢掉, op 那边再报「用 force 才能替换」.
        """
        if not self.ctx or not card:
            return False
        from mdstudio.core.gate import CutinGate
        try:
            return int(card) in CutinGate(self.ctx.game).mrks()
        except Exception:
            return False

    def _ask_replace(self, card: int):
        """目标卡自带官方动画时问一次要不要覆盖

        返回 True=覆盖, False=用户取消, None=本来就没有官方动画.
        覆盖只是把官方那几个 bundle 换成我们的 (Tx 记进 modified 并备份原文件),
        之后点「去除」会按备份还原, 官方那套回来.
        """
        if not self._is_official(card):
            return None
        name = self.db.name(card) if self.db else ""
        who = f"{name} · {card}" if name else str(card)
        ok = QMessageBox.question(
            self, tr("Replace the game's cut-in?"),
            tr("%s already has a cut-in made by the game. Continuing replaces it: the game's "
               "version stops playing and yours plays instead.\n\nRemove brings the game's "
               "version back.") % who,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return ok == QMessageBox.Yes

    def _sync_target_hint(self):
        """目标卡换了的联动: 官方卡锁死来源与克隆, 普通卡放开

        官方自带动画的卡只走「视频重建 / Spine 导入」来改它: 自我克隆产不出可改副本
        (23 个文件语义零变化), 只是留一条认领记录, 所以不让人点.
        它也不接受别的来源卡 (那样等于拿别人的动画顶掉官方演出).
        """
        target = self.cut_card.peek()
        official = self._cut_force()
        self.cut_official.setVisible(official)
        # 已经有我们的动画时也不能再克隆: op 那边会拦「already has a cut-in from this tool」,
        # 按钮亮着只会让人点出一个报错
        self.cut_btn.setEnabled(not official and not self._cut_has_ours())
        self.cut_donor.setReadOnly(official)
        self.cut_donor_preset.setEnabled(not official)
        if official:
            # 官方卡: 来源固定是它自己, 用户改不了
            if self.cut_donor.peek() != target:
                self.cut_donor.setValue(target)
                self._auto_donor = target        # 记住这是我们填的, 不是用户选的
            self.cut_hint.setText(tr("This will replace the game's own cut-in."))
        else:
            if self._auto_donor and self.cut_donor.peek() == self._auto_donor:
                self.cut_donor.setText("")       # 换到普通卡就把它带走, 别留上一个目标
            self._auto_donor = None
            if self._cut_has_ours():
                # 已经有我们的动画: 克隆按钮是灰的, 再说「来源卡不受影响」会让人以为还能点
                self.cut_hint.setText(tr("This card already has our cut-in. Remove it before cloning again."))
            else:
                self.cut_hint.setText(tr("The donor is left untouched."))

    def on_donor_preset(self, _i=0):
        """选预设就填进源卡输入框; setValue 会在名字加载后自动补成「名字 · id」"""
        cid = self.cut_donor_preset.currentData()
        if cid:
            self.cut_donor.setValue(int(cid))

    def on_cut_clone(self):
        from mdstudio.ops import cutin as C
        card = self.cut_card.value()
        force = self._ask_replace(card)
        if force is False:
            return
        self.run(C.clone, card, self.cut_donor.value(), force=bool(force))

    def on_cut_video(self):
        from mdstudio.ops.video import cutin_flip
        src = self.cv_src.text()
        if not src:
            QMessageBox.information(self, tr("No source"), tr("Pick a video, GIF or frame folder first."))
            return
        # 目标卡自带官方动画时先问, 用户在真正开跑之前决定要不要覆盖
        card = self.cut_card.value()
        force = self._ask_replace(card)
        if force is False:
            return
        preset = self.cv_preset.currentData()
        try:
            if preset:
                curve = cutin_flip.parse_pop_curve(preset)
            else:
                curve_txt = self.cv_curve.text().strip()
                curve = cutin_flip.parse_pop_curve(curve_txt) if curve_txt else None
        except MDStudioError as e:
            QMessageBox.warning(self, tr("Didn't work"), str(e))
            return
        self.run(cutin_flip.apply, card, src, n=self.cv_frames.value(), cell=self.cv_cell.value(),
                 duration=self.cv_dur.value(), size=self.cv_size.value(), mode="auto",
                 sharp=self.cv_sharp.isChecked(), sharp_model=self.cv_sharp_model.currentData(),
                 pop=self.cv_pop.isChecked(), pop_curve=curve,
                 offset_x=self.cv_offx.value(), offset_y=self.cv_offy.value(), zoom=self.cv_zoom.value(),
                 rotate=self.cv_rot.value(), donor=self.cut_donor.value(), force=bool(force))

    def _spine_src(self):
        src = self.mp_src.text()
        if not src:
            QMessageBox.information(self, tr("No source"),
                                    tr("Pick a Spine export, bundle, zip or folder first."))
        return src

    def on_spine_card(self):
        from mdstudio.ops import spine as M
        src = self._spine_src()
        if not src:
            return
        card = self.cut_card.value()
        force = self._ask_replace(card)
        if force is False:
            return
        self.run(M.port_card, card, src, force=bool(force))

    def on_spine_all(self):
        from mdstudio.ops import spine as M
        src = self._spine_src()
        if src and self._confirm(tr("Import every Spine cut-in in this folder to cards that don't have one "
                                    "yet? This takes a while (about 10 s per card).")):
            self.run(M.port_all, src, force=False)      # 批量导入只补没有动画的卡

    def on_spine_check(self):
        from mdstudio.ops import spine as M
        src = self._spine_src()

        def show(res):
            bad = [(m, n, p) for m, n, p in (res or []) if p]
            for m, n, p in bad:
                log.warning(tr("%s %s: %s"), m, n, "; ".join(p))
            log.info(tr("%d installed, %d with problems"), len(res or []), len(bad))
        if src:
            self.run(M.check_all, src, done=show)

    def on_reskin_export(self):
        from mdstudio.ops import cutin as C
        if not self.need_game():
            return
        card = self.cut_card.value()
        d = QFileDialog.getExistingDirectory(self, tr("Export to"))
        if d:
            self.run(C.reskin_export, card, d,
                     done=lambda files: log.info(tr("exported %d texture(s) to %s"), len(files or []), d))

    def on_reskin_import(self):
        from mdstudio.ops import cutin as C
        files, _ = QFileDialog.getOpenFileNames(self, tr("Painted textures"), "", "PNG (*.png)")
        if files:
            self.run(C.reskin_import, self.cut_card.value(), files)

    # ------------------------------------------------------------------ run-effect
    def _tab_runfx(self):
        from mdstudio.ops import runfx as R
        w = QWidget()
        h = QHBoxLayout(w)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)      # 页面布局已有 9px, 这里再加两栏会错位
        left.setSpacing(10)
        self.fx_list = QListWidget()
        self.fx_list.itemClicked.connect(lambda it: self.fx_card.setValue(it.data(Qt.UserRole)))
        left.addWidget(_list_card(tr("Your effects"), self.fx_list))
        self.fx_filter = QLineEdit()
        self.fx_filter.setPlaceholderText(tr("filter…"))
        self.fx_filter.textChanged.connect(self._filter_donors)
        # 来源卡这块也包成卡片, 否则左栏下半截是裸控件, 跟右侧的卡片视觉上不齐
        donor_card = QWidget()
        donor_card.setObjectName("Card")
        dc = QVBoxLayout(donor_card)
        dc.setContentsMargins(12, 10, 12, 12)
        dc.setSpacing(8)
        dc_head = QLabel(tr("Donors (every card with an effect)"))
        dc_head.setObjectName("CardTitle")
        dc.addWidget(dc_head)
        dc.addWidget(self.fx_filter)
        self.fx_donors = QTableWidget(0, 5)
        self.fx_donors.setHorizontalHeaderLabels([tr("Card"), tr("Name"), tr("When"), tr("Sound"), tr("Effect")])
        _fit_columns(self.fx_donors)
        # 左栏只有 360px 上下, 5 列必然横向溢出. 音效与特效是内部标识, 挑来源卡用不上,
        # 收进名称列的 tooltip; 列仍保留, 过滤与取数都还走这五列
        self.fx_donors.setColumnHidden(3, True)
        self.fx_donors.setColumnHidden(4, True)
        self.fx_donors.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.fx_donors.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.fx_donors.verticalHeader().hide()
        self.fx_donors.cellClicked.connect(lambda r, _: self.fx_donor.setValue(int(self.fx_donors.item(r, 0).text())))
        self.fx_donors.setObjectName("Flat")
        dc.addWidget(self.fx_donors, 1)
        left.addWidget(donor_card, 2)
        h.addLayout(left, 3)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        g = QGroupBox(tr("Borrow an effect"))
        f = QFormLayout(g)
        self.fx_card = _card_spin()
        self.fx_donor = _card_spin(4842, "donor")
        self.fx_replace = QCheckBox(tr("Replace the game's own effect"))
        f.addRow(tr("Card ID"), self.fx_card)
        f.addRow(tr("Donor card ID"), self.fx_donor)
        f.addRow("", self.fx_replace)
        f.addRow(_row(_btn(tr("Borrow"), lambda: self.run(R.borrow, self.fx_card.value(), self.fx_donor.value(),
                                                      replace=self.fx_replace.isChecked()), "Primary"),
                      _btn(tr("Remove"), self.on_fx_remove, "Danger"), 1))
        # 每次发动都会播, 多效果的卡会每个效果都播一遍; timing 跟着 donor 走, chain 最稳
        tip = QLabel(tr("Best on cards with one effect, monster or spell/trap (like Konami's Monster Reborn "
                        "or Divine Arsenal AA-ZEUS - Sky Thunder): it plays on every activation. Pick a "
                        "donor whose 'When' is chain; negated / destroyed / moved only fire on that event."))
        tip.setWordWrap(True)
        tip.setObjectName("Muted")
        f.addRow(tip)
        right.addWidget(g)

        g = QGroupBox(tr("From a video (fullscreen works, with sound)"))
        f = QFormLayout(g)
        self.fv_src = FilePick(tr("Video"), tr("Video (*.mp4 *.webm *.mov *.gif);;All (*)"), allow_folder=True)
        self.fv_mode = _mode_box()
        self.fv_frames = QSpinBox()
        self.fv_frames.setRange(4, 200)
        self.fv_frames.setValue(120)
        self.fv_cell = QSpinBox()
        self.fv_cell.setRange(128, 2048)
        self.fv_cell.setValue(1080)
        self.fv_dur = QDoubleSpinBox()
        self.fv_dur.setRange(0.5, 15.0)
        self.fv_dur.setValue(5.0)
        # 声音也给下拉: 只列盘上有的 (新 label 不响)
        self.fv_sound = QComboBox()
        self.fv_sound.setEditable(True)
        self.fv_sound.lineEdit().setPlaceholderText(tr("type to search a sound already on disk (empty = silent)"))
        # 视频自带声音: 抽出来塞进上面选的 label (新 label 不响, 只能借一个盘上有的)
        self.fv_audio = QCheckBox(tr("Use the video's own audio (goes into the sound label below)"))
        self.fv_audio.setChecked(True)
        self.fv_users = QLabel("")
        self.fv_users.setWordWrap(True)
        self.fv_users.setObjectName("Muted")
        self.fv_sound.currentTextChanged.connect(lambda _t: self._label_users(self.fv_sound, self.fv_users))
        self.fv_audio.toggled.connect(lambda _on: self._label_users(self.fv_sound, self.fv_users))
        f.addRow(tr("Source"), self.fv_src)
        f.addRow(tr("Mode"), self.fv_mode)
        f.addRow(_row(QLabel(tr("Frames")), self.fv_frames, QLabel(tr("Frame height px")), self.fv_cell,
                      QLabel(tr("Seconds")), self.fv_dur, 1))
        f.addRow("", self.fv_audio)
        f.addRow(tr("Sound"), self.fv_sound)
        f.addRow("", self.fv_users)
        lim = QLabel(tr("Limit: 16384 px per side. 1080p = 120 frames max (8 x 15), ~22 fps over 5.5 s."))
        lim.setObjectName("Muted")
        f.addRow(lim)
        f.addRow(_row(1, _btn(tr("Build effect"), self.on_fx_video, "Primary")))
        right.addWidget(g)

        g = QGroupBox(tr("Framing (no rebuild)"))
        f = QHBoxLayout(g)
        self.fx_size = QDoubleSpinBox()
        self.fx_size.setRange(0.1, 3.0)
        self.fx_size.setSingleStep(0.02)
        self.fx_size.setValue(1.08)
        f.addWidget(QLabel(tr("Height on screen (1.0 = full height)")))
        f.addWidget(self.fx_size)
        f.addWidget(_btn(tr("Apply"), self.on_fx_framing))
        f.addStretch(1)
        right.addWidget(g)

        # 只是 run effect 的音效 (seLabel), cut-in 声音不在这
        g = QGroupBox(tr("Activation effect sound (replace with your WAV)"))
        f = QFormLayout(g)
        self.fs_label = QComboBox()
        self.fs_label.setEditable(True)
        self.fs_label.lineEdit().setPlaceholderText(tr("pick a card above, its sound fills in here"))
        self.fs_wav = FilePick(tr("WAV or video"), tr("WAV or video (*.wav *.mp4 *.webm *.mov *.mkv);;All (*)"))
        # 选了卡就自动填它现在的特效音, 不用自己找; 换不了的 (内置) 直接在这说
        self.fs_card_note = QLabel("")
        self.fs_card_note.setWordWrap(True)
        self.fs_users = QLabel("")
        self.fs_users.setWordWrap(True)
        self.fs_users.setObjectName("Muted")
        f.addRow("", self.fs_card_note)
        f.addRow(tr("Sound"), self.fs_label)
        f.addRow("", self.fs_users)
        f.addRow(tr("WAV / video"), self.fs_wav)
        self.fx_card.textChanged.connect(lambda _t: self._auto_sound())
        self.fs_label.currentTextChanged.connect(lambda _t: self._sound_users())
        note = QLabel(tr("Every card whose activation effect uses this sound changes too. "
                         "Cut-in sounds aren't here."))
        note.setWordWrap(True)
        note.setObjectName("Muted")
        f.addRow(note)
        f.addRow(_row(1, _btn(tr("Replace sound"), self.on_fs_inject, "Primary"),
                      _btn(tr("Restore original"), self.on_fs_restore)))
        right.addWidget(g)
        right.addStretch(1)
        area = _scroll(right)
        area.setMinimumWidth(560)        # 表单再窄就会被右边裁掉, 让表格那侧让步
        h.addWidget(area, 2)
        return w

    def on_fx_remove(self):
        from mdstudio.ops import runfx as R
        c = self.fx_card.value()
        if self._confirm(tr("Remove the effect this tool added to %s?") % c):
            self.run(R.remove, c)

    def on_fx_video(self):
        from mdstudio.ops.video import runfx_flip
        src = self.fv_src.text()
        if not src:
            QMessageBox.information(self, tr("No source"), tr("Pick a video first."))
            return
        lb = self.fv_sound.currentText().strip()
        use_audio = self.fv_audio.isChecked()
        if use_audio and not lb:
            QMessageBox.information(self, tr("Pick a sound label"),
                                    tr("The video's audio needs an existing sound label to ride on "
                                       "(new labels are silent). Pick one in Sound, or untick the audio box."))
            return
        self.run(runfx_flip.apply, self.fx_card.value(), src, n=self.fv_frames.value(), cell=self.fv_cell.value(),
                 duration=self.fv_dur.value(), mode=self.fv_mode.currentData(), se_label=lb,
                 video_audio=use_audio)

    def on_fx_framing(self):
        from mdstudio.ops.video import runfx_flip
        self.run(runfx_flip.set_framing, self.fx_card.value(), self.fx_size.value())

    def _auto_sound(self):
        from mdstudio.core.gate import RunFxGate
        from mdstudio.ops import sound as S
        if not self.ctx or not hasattr(self, "fs_card_note"):
            return
        c = self.fx_card.peek()
        if not c:
            self.fs_card_note.setText("")
            return
        try:
            labels = S.labels_of_card(RunFxGate(self.ctx.game), c)
        except MDStudioError as e:
            self.fs_card_note.setText(f"<span style='color:#e0a030'>{e}</span>")
            return
        from mdstudio.ops import runfx as R
        ours = bool(R._ours(self.ctx, c))
        has_fx = bool(RunFxGate(self.ctx.game).for_card(c))
        ok = [lb for lb in labels if S.on_disk(self.ctx.game, lb)]
        if ok:
            self.fs_label.setCurrentText(ok[0])
            more = tr(" (also: %s)") % ', '.join(ok[1:]) if len(ok) > 1 else ""
            self.fs_card_note.setText(tr("<span style='color:#3fb27f'>This card's effect plays %s%s. "
                                         "Pick a WAV (or the video itself) and press Replace.</span>")
                                      % (ok[0], more))
            return
        self.fs_label.setCurrentText("")
        why = S.why_missing(labels[0]) + " " if labels else tr("Its effect has no sound. ")
        if ours:
            # 自己加的特效: 挑一个盘上有的 label, Replace 时顺手把这张卡换成用它
            self.fs_card_note.setText(tr("<span style='color:#e0a030'>%sPick a sound in the list (it's "
                                         "borrowed, see below who else uses it), pick a WAV or the video, "
                                         "press Replace: this card switches to that sound.</span>") % why)
        elif has_fx:
            self.fs_card_note.setText(f"<span style='color:#e0a030'>{why}</span>")
        else:
            self.fs_card_note.setText(tr("<span style='color:#8a8f98'>This card has no activation effect "
                                         "yet. Add one first (borrow or video).</span>"))

    def _sound_users(self):
        if hasattr(self, "fs_users"):
            self._label_users(self.fs_label, self.fs_users)

    def _label_users(self, box, out):
        # 选了哪个 label 就说一下还有谁在用 (换了它们也跟着变)
        from mdstudio.ops import runfx as R
        lb = box.currentText().strip()
        if not self.ctx:
            out.setText("")
            return
        if not lb:
            silent = box is getattr(self, "fv_sound", None) and not self.fv_audio.isChecked()
            out.setText(tr("No sound.") if silent else
                        tr("<span style='color:#e0a030'>Pick a sound label to carry the audio "
                           "(new labels are silent, so an existing one is borrowed).</span>"))
            return
        cards = sorted({m for m, _t, se, _e, _o in R.list_effects(self.ctx.game) if se == lb})
        names = [f"{self.db.name(m) if self.db else ''} ({m})".strip() for m in cards[:6]]
        more = f" +{len(cards) - 6}" if len(cards) > 6 else ""
        out.setText(tr("Also used by: %s%s. Replacing it changes theirs too.")
                    % (', '.join(names), more) if cards else "")

    def on_fs_inject(self):
        from mdstudio.ops import sound as S
        lb, wav = self.fs_label.currentText().strip(), self.fs_wav.text()
        if not lb or not wav:
            QMessageBox.information(self, tr("Missing"), tr("Pick a sound and a WAV file first."))
            return
        self.run(_switch_and_inject, self.fx_card.peek(), lb, wav)

    def on_fs_restore(self):
        from mdstudio.ops import sound as S
        lb = self.fs_label.currentText().strip()
        if lb:
            self.run(S.restore, lb)

    def _filter_donors(self, text):
        t = text.lower()
        for r in range(self.fx_donors.rowCount()):
            # 音效列显示的是截短后的名字, 检索要连 tooltip 里的全文一起查, 不然搜 SE_EV_ 搜不到
            row = " ".join(f"{self.fx_donors.item(r, c).text()} "
                           f"{self.fx_donors.item(r, c).toolTip()}" for c in range(5)).lower()
            self.fx_donors.setRowHidden(r, t not in row)

    # ------------------------------------------------------------------ packs
    def _tab_pack(self):
        w = QWidget()
        h = QHBoxLayout(w)
        g = QGroupBox(tr("Export your work as a pack"))
        v = QVBoxLayout(g)
        self.pack_cards = QListWidget()
        self.pack_cards.setSelectionMode(QAbstractItemView.MultiSelection)
        v.addWidget(QLabel(tr("Pick cards (nothing selected = all). Every dependency is included.")))
        v.addWidget(self.pack_cards)
        self.pack_name = QLineEdit("my-pack")
        v.addWidget(_row(QLabel(tr("Name")), self.pack_name, _btn(tr("Export…"), self.on_pack_export, "Primary")))
        h.addWidget(g, 1)

        g = QGroupBox(tr("Install a pack"))
        v = QVBoxLayout(g)
        v.addWidget(QLabel(tr("Zip or folder made by this tool. Cards that already have a game "
                              "cut-in are skipped.")))
        v.addWidget(_btn(tr("Install pack…"), self.on_pack_install, "Primary"),
                    0, Qt.AlignLeft)
        v.addStretch(1)
        h.addWidget(g, 1)
        return w

    def on_pack_export(self):
        from mdstudio.ops import pack as P
        if not self.need_game():
            return
        d = QFileDialog.getExistingDirectory(self, tr("Export to"))
        if not d:
            return
        cards = [it.data(Qt.UserRole) for it in self.pack_cards.selectedItems()]
        cards = cards or sorted(P.exportable(self.ctx.journal))
        self.run(P.export, cards, d, name=self.pack_name.text().strip() or "my-pack",
                 done=lambda path: log.info(tr("pack -> %s"), path))

    def on_pack_install(self):
        from mdstudio.ops import pack as P
        if not self.need_game():
            return
        p, _ = QFileDialog.getOpenFileName(self, tr("Pack"), "", tr("Pack (*.zip);;All (*)"))
        if p:
            self.run(P.install, p, done=lambda r: log.info(tr("installed: %s"), r))

    # ------------------------------------------------------------------ history
    def _tab_history(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.hist = QTableWidget(0, 4)
        self.hist.setHorizontalHeaderLabels([tr("Time"), tr("What"), tr("Card"), tr("From")])
        _fit_columns(self.hist)
        self.hist.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hist.verticalHeader().hide()
        v.addWidget(self.hist)
        self.hist_note = QLabel(tr("Journal + backups: -"))
        self.hist_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.hist_note)
        return w

    # ------------------------------------------------------------------ settings
    def _tab_settings(self):
        # 三个分组加起来比页面高, 直接塞进页面会被 Qt 压到最小高以下 (行高塌成十几像素,
        # 文字被切一半). 包一层滚动区, 矮的时候出滚动条而不是压扁
        v = QVBoxLayout()
        v.setSpacing(8)

        g = QGroupBox(tr("Game folder"))
        f = QFormLayout(g)
        self.set_dir = QLineEdit()
        self.set_dir.setReadOnly(True)
        self.set_dir.setMinimumWidth(360)
        self.set_dir.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_dir.setToolTip(tr("The folder that contains LocalData. Detected from Steam "
                                   "automatically; browse to it if detection fails."))
        f.addRow(tr("Folder"), _row(self.set_dir,
                                    _btn(tr("Browse…"), self._choose_game),
                                    _btn(tr("Detect again"), self.on_auto_detect), 1))

        self.set_account = QComboBox()
        self.set_account.setToolTip(tr("Each account has its own copy of the game data. Changes "
                                       "only apply to the account picked here."))
        self.set_account.currentIndexChanged.connect(self.on_account_pick)
        f.addRow(tr("Account"), self.set_account)

        self.set_note = QLineEdit()
        self.set_note.setPlaceholderText(tr("optional, e.g. main account"))
        self.set_note.setToolTip(tr("Shown next to the account hash in the title bar. "
                                    "Saves as soon as you leave the field."))
        self.set_note.editingFinished.connect(self.on_note_name)
        f.addRow(tr("Note name"), self.set_note)

        note = QLabel(tr("Each account keeps its own copy of the files; history is per account too."))
        note.setWordWrap(True)
        note.setObjectName("Muted")
        f.addRow(note)
        v.addWidget(g)

        g = QGroupBox(tr("Dry run"))
        f = QFormLayout(g)
        self.dry = QCheckBox(tr("Dry run"))
        self.dry.setToolTip(tr("Dry run: nothing is written to the game. The log lists every file "
                               "and byte count that would change, so you can check before committing. "
                               "A badge stays in the title bar while the mode is on — click it to "
                               "come back here."))
        self.dry.stateChanged.connect(lambda _: self._on_dry_toggle(self.dry.isChecked()))
        f.addRow("", self.dry)
        dry_note = QLabel(tr("Writes nothing: the log only lists what would change. Exports still work."))
        dry_note.setObjectName("Muted")
        f.addRow("", _row(self.dry, dry_note, 1))
        v.addWidget(g)

        g = QGroupBox(tr("Maintenance"))
        f = QFormLayout(g)
        self.set_journal = QLabel("-")
        self.set_journal.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.set_journal.setWordWrap(True)
        f.addRow(tr("History stored in"), self.set_journal)
        f.addRow(_row(_btn(tr("Resync"), self.on_resync, "Primary"), 1))
        tip2 = QLabel(tr("A game update swaps the data folder, so your changes stop showing."))
        tip2.setWordWrap(True)
        tip2.setObjectName("Muted")
        f.addRow(tip2)
        v.addWidget(g)

        v.addStretch(1)
        return _scroll(v)

    # ------------------------------------------------------------------ refresh / resync
    def on_resync(self):
        from mdstudio.ops.tx import resync
        if not self.need_game():
            return

        def show(r):
            log.info(tr("resync: cut-ins %s, effects %s, manifest fixes %d, moved %d, lost %d"),
                     r["cutin"] or "-", r["runfx"] or "-", r["catalog"], len(r.get("moved", [])),
                     len(r["lost_files"]))
            # 要用户动手的单独弹出来, 只进日志没人看
            todo = [tr("Files lost (make that card again): %s") % f for f in r["lost_files"]]
            todo += [tr("Card %s: files missing, remove it and add it again") % c for c in r.get("broken", {})]
            todo += [tr("Card %s: the game now has its own version, yours was left off")
                     for c in r.get("game_took_over", [])]
            todo += [tr("Activation effect sound %s: updated by the game, your WAV is gone "
                        "(restore, then replace again)") for lb in r.get("sound_reset", [])]
            if todo:
                QMessageBox.warning(self, tr("Resync: needs a look"), "\n".join(todo[:20]))
        self.run(resync, done=show)

    def refresh(self):
        if not self.ctx or self._busy:
            return
        from mdstudio.ops import cutin as C
        from mdstudio.ops import pack as P
        from mdstudio.ops import runfx as R
        self._inv = None
        try:
            t = self.stack.currentWidget()
            if t is self.t_cards:
                self._card_search()
            elif t is self.t_cutin:
                rows = C.list_cards(self.ctx.game, self.ctx.journal)
                # 官方自带动画的卡: 克隆时来源卡默认填它自己, 并挂一个「官方有动画」标签
                self._official = {card for card, st in rows if st == "game"}
                # 已经有我们动画的卡 (yours / wiped): 再点克隆会被 op 拦, 按钮该是灰的
                self._mine = {card for card, st in rows if st in ("yours", "wiped")}
                self._fill_cards(self.cut_list, rows)
                self._sync_target_hint()
            elif t is self.t_runfx:
                self._fill_cards(self.fx_list, R.list_cards(self.ctx.game, self.ctx.journal))
                # 卡名加载完之前填的表没有名字, 加载完再填一次
                if not self.fx_donors.rowCount() or (self.db and not self.fx_donors.item(0, 1).text()):
                    self._fill_donors(R.list_effects(self.ctx.game))
            elif t is self.t_pack:
                self.pack_cards.clear()
                for card, kinds in sorted(P.exportable(self.ctx.journal).items()):
                    it = QListWidgetItem(f"{card}  {self.db.name(card) if self.db else ''}   {' + '.join(sorted(kinds))}")
                    it.setData(Qt.UserRole, card)
                    self.pack_cards.addItem(it)
            elif t is self.t_hist:
                recs = self.ctx.journal.load()
                self.hist.setRowCount(len(recs))
                for r, rec in enumerate(reversed(recs)):
                    src = rec.meta.get("donor") or rec.meta.get("label") or rec.meta.get("pack") or \
                        rec.meta.get("source") or ""
                    for c, val in enumerate((time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.time)),
                                             rec.op, f"{rec.card} {self.db.name(rec.card) if self.db else ''}".strip() if rec.card else "",
                                             src)):
                        self.hist.setItem(r, c, _cell(val))
                _autosize(self.hist)
            elif t is self.t_set:
                self._sync_settings_page()
        except MDStudioError as e:
            log.error(str(e))

    def _fill_cards(self, lst, rows):
        lst.clear()
        for card, status in rows:
            if status == "game":
                continue
            name = self.db.name(card) if self.db else ""
            it = QListWidgetItem()
            it.setData(Qt.UserRole, card)
            lst.addItem(it)
            row = pill.PillRow(card, name, STATUS_TEXT[status], STATUS_COLOR[status])
            it.setSizeHint(QSize(0, max(row.sizeHint().height(), 34)))   # 行太矮会把标签压扁
            lst.setItemWidget(it, row)

    def _fill_donors(self, rows):
        self.fx_donors.setRowCount(len(rows))
        for r, (mrk, when, se, ep, ok) in enumerate(rows):
            eff = (ep.rsplit("/", 1)[-1] if ep else tr("(built-in)")) + \
                  ("" if ok in (None, True) else tr("  [not downloaded]"))
            # 音效与特效两列默认隐藏 (左栏放不下 5 列), 全文挂在名称的 tooltip 上
            name = self.db.name(mrk) if self.db else ""
            tip = f"{name}\n{tr('Sound')}: {se or '-'}\n{tr('Effect')}: {ep or eff}".strip()
            for c, val in enumerate((mrk, name, tr(when), se or "-", eff)):
                self.fx_donors.setItem(r, c, _cell(val, tip if c == 1 else None))
        _autosize(self.fx_donors)


def _crash(exc_type, exc, tb):
    # pythonw 没有控制台, 崩了什么都看不到. 写日志 + 弹窗, 不然用户只会说 "点了没反应"
    import traceback
    from mdstudio.core.journal import home_dir
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        p = home_dir() / "gui_crash.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except Exception:
        # home_dir() 在程序目录不可写时会抛 RuntimeError, 崩溃处理不能跟着崩
        p = None
    msg = f"{exc_type.__name__}: {exc}\n\n{tr('Log: %s') % p}"
    if QApplication.instance():
        QMessageBox.critical(None, tr("MD Cut-in Kit crashed"), msg)
    else:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, tr("MD Cut-in Kit crashed"), 0x10)


def main(argv=None):
    sys.excepthook = _crash
    try:
        app = QApplication(sys.argv if argv is None else argv)
        theme.apply_font(app)
        theme.apply_translation(app)      # 对话框的标准按钮跟着中文走
        wheelguard.install(app)      # 滚轮不改值, 只滚页面
        w = Main(os.environ.get("MDSTUDIO_GAME"))
        w.show()
        w.raise_()
        w.activateWindow()   # 从 bat 启动时窗口常被压在资源管理器后面
        return app.exec()
    except Exception:
        _crash(*sys.exc_info())
        return 1


if __name__ == "__main__":
    sys.exit(main())
