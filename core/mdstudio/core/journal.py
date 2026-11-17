"""操作记录. 卸载只撤销自己加的东西, 不整文件覆盖回去

为什么: manifest_tcg 是公用的 (壁纸 mod 也写它), 游戏更新也会重写.
拿旧备份整个盖回去 = 把别人的条目/新版本的条目一起删了.
"""
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from mdstudio import runtime
from mdstudio.core.baseline import BASELINE, Baselines, family_for
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr


RENAMED_OPS = {"cutin.mdpro3": "cutin.spine"}

# 活动计数只留这么多天（比界面上的 30 天多一倍，切账号/跨月都够用），文件不会越写越大
ACTIVITY_KEEP_DAYS = 60


def activity_day(ts: float) -> str:
    """本地时间的 YYYY-MM-DD。热力图按天聚合用这个，别用 UTC，不然晚上十点后的算到第二天。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def home_dir() -> Path:
    """程序数据目录 (操作记录 / 文件备份 / 崩溃日志)

    落在程序目录下的 data, 这样整个文件夹搬到别的机器也能带着历史和撤销能力.
    MDSTUDIO_HOME 可以覆盖 (想放到别的盘时用).
    """
    new = runtime.data_dir()
    _adopt_legacy(new)
    return new


def _adopt_legacy(new: Path) -> None:
    """老版本把记录和备份放在 LOCALAPPDATA, 第一次跑就整个搬进程序目录"""
    if new.exists():
        return
    old = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MDStudio"
    if not old.is_dir():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
    except OSError:
        # 搬不动就照常跑 (新的空目录), 别因为迁移失败把启动卡死
        pass


def _adopt_group(base: Path, game) -> None:
    """把旧版存在 base 根上的记录搬进「账号」子目录

    旧版按安装存 (base/journal.json), 新版按 安装+账号 存 (base/<account>/).
    升级后不能把历史留在一个谁都不读的地方, 所以这里搬一次.

    归属判断不靠猜: 记录里写着它改过哪些相对路径, 拿这些路径去比对各账号目录,
    哪个账号真有这些文件就归给哪个. 升级前只有某一个账号被改过, 所以那些记录会自动
    落到那个账号名下. 一个都对不上才归给当前选中的账号.
    """
    legacy = base / "journal.json"
    if not legacy.is_file():
        return
    target = base / _guess_owner(base, game)
    if target.exists():
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        legacy.replace(target / "journal.json")
        old_backups = base / "backups"
        if old_backups.is_dir():
            old_backups.replace(target / "backups")
    except OSError:
        pass          # 搬不动就算了, 别把启动卡死


def _guess_owner(base: Path, game) -> str:
    """按记录里写过的文件路径判断它属于哪个账号"""
    try:
        raw = json.loads((base / "journal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return game.group
    rels = set()
    for r in raw if isinstance(raw, list) else []:
        rels |= set(r.get("created") or [])
        rels |= set((r.get("modified") or {}).keys())
    if not rels:
        return game.group
    try:
        accounts = game.accounts()
    except OSError:
        return game.group
    best, best_hits = game.group, 0
    for acc in accounts:
        root = game.localdata / acc / "0000"
        hits = sum(1 for r in rels if (root / r).is_file())
        if hits > best_hits:
            best, best_hits = acc, hits
    return best


@dataclass
class Record:
    op: str
    card: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    time: float = field(default_factory=time.time)
    created: list = field(default_factory=list)       # 新建的 bundle, 相对 root 的路径
    modified: dict = field(default_factory=dict)      # 被改的已有文件 -> 备份文件名
    catalog: dict = field(default_factory=dict)       # assetName -> {"new": deps, "prev": deps|None}
    cutin_mrks: list = field(default_factory=list)
    runfx_added: list = field(default_factory=list)   # 完整 entry
    runfx_removed: list = field(default_factory=list)  # 替换模式下删掉的旧 entry, 卸载时还原
    meta: dict = field(default_factory=dict)


class Journal:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.file = self.folder / "journal.json"
        self.backups = self.folder / "backups"
        # 基线层与 journal 同级，跟着账号走：一张卡的某一类改动只留一份「动手前的样子」
        self.baselines = Baselines(self.folder / "baselines")

    @classmethod
    def for_game(cls, game):
        """按 安装 + 账号 分开

        同一台机器上多个账号各自的 bundle 是两份独立副本, 相对路径一样但内容
        不同. 共用一份记录会让撤销/同步拿着 A 账号的路径去写 B 账号, 所以必须分开.
        """
        base = home_dir() / game.install_id
        _adopt_group(base, game)
        return cls(base / game.group)

    def load(self) -> list:
        if not self.file.is_file():
            return []
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except ValueError as e:
            raise MDStudioError(tr("journal is damaged: %s (%s). Restore it from journal.json.tmp "
                                  "or remove it (the tool then forgets what it added)")
                               % (self.file, e)) from None
        known = set(Record.__dataclass_fields__)
        for r in raw:
            r["op"] = RENAMED_OPS.get(r.get("op"), r.get("op"))
        # 新版本可能多存了字段, 旧版本读的时候忽略掉
        return [Record(**{k: v for k, v in r.items() if k in known}) for r in raw]

    def _save(self, records):
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in records], indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.file)

    def commit(self, rec: Record):
        recs = self.load()
        recs.append(rec)
        self._save(recs)
        self.bump_activity()

    def update(self, changed):
        by_id = {r.id: r for r in changed}
        self._save([by_id.get(r.id, r) for r in self.load()])

    def drop(self, ids):
        ids = set(ids)
        recs = self.load()
        self._save([r for r in recs if r.id not in ids])
        for i in ids:
            shutil.rmtree(self.backups / i, ignore_errors=True)

    def for_card(self, card: int, ops=None) -> list:
        return [r for r in self.load() if r.card == card and (ops is None or r.op in ops)]

    # ------------------------------------------------------------ 活动计数
    # 热力图要的是「哪天干了多少活」，而记录会被复原删掉。所以单独存一份只增不减的
    # 每日计数，跟 journal 放在同一个账号目录里：切账号 = 换一张热力图。
    def _activity_file(self) -> Path:
        return self.folder / "activity.json"

    def _save_activity(self, days: dict) -> None:
        """写盘。顺带剪掉超过 ACTIVITY_KEEP_DAYS 的旧日期，文件不会无限长。

        日期是 YYYY-MM-DD，字符串比较就是日期比较，不用解析。
        """
        cutoff = (date.today() - timedelta(days=ACTIVITY_KEEP_DAYS)).isoformat()
        keep = {d: int(n) for d, n in days.items() if d >= cutoff and int(n) > 0}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self._activity_file().with_suffix(".tmp")
            tmp.write_text(json.dumps({"days": keep}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._activity_file())
        except OSError:
            pass          # 写不进就算了，热力图少一天不影响任何功能

    def activity(self) -> dict:
        """{"2026-11-16": 3, ...}。文件不存在时用现有记录回填一次。"""
        f = self._activity_file()
        if f.is_file():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            days = raw.get("days") if isinstance(raw, dict) else None
            if not isinstance(days, dict):
                return {}
            return {str(k): int(v) for k, v in days.items()
                    if isinstance(v, (int, float)) and int(v) > 0}
        # 第一次跑：拿还在的记录补一遍，否则老用户的热力图是空白一片
        days: dict = {}
        try:
            for r in self.load():
                d = activity_day(r.time)
                days[d] = days.get(d, 0) + 1
        except MDStudioError:
            days = {}          # 记录坏了不该连带热力图一起罢工
        self._save_activity(days)
        return days

    def bump_activity(self) -> None:
        """记一次折腾（改游戏文件或复原）。"""
        days = self.activity()
        d = date.today().isoformat()
        days[d] = days.get(d, 0) + 1
        self._save_activity(days)

    def activity_days(self, n: int = 30) -> list:
        """最近 n 天，从早到晚：[{"date": "2026-10-18", "count": 0}, ...]"""
        days = self.activity()
        today = date.today()
        return [{"date": d.isoformat(), "count": int(days.get(d.isoformat(), 0))}
                for d in (today - timedelta(days=i) for i in range(n - 1, -1, -1))]

    def backup(self, rec: Record, src: Path, rel: str, dry_run=False):
        """记下这个文件被改之前的样子。

        基线层（2026-11-13）：一张卡的某一类改动只留第一份。以前每条记录各存一份，
        第二份的内容是上一次的输出 —— 撤销按时间倒序还原，后写的盖先写的，那份
        永远读不到，只是白占磁盘（一次召唤动画约 238MB）。现在第一次碰这个文件
        时存进 `baselines/<卡>/<族>/`，之后同一族的写入只打标记不复制。
        """
        # 同一条记录里同一文件只备份第一次 (= 改动前的样子)
        if rel in rec.modified:
            return
        # 安装根目录下的大文件(如 data.unity3d)：只备份一次「真原图」到共享位置，
        # 不进每条记录各自的备份目录（否则每次替换都拷一份上 GB 的文件，很快撑爆磁盘）。
        if rel.startswith("__install__/"):
            rec.modified[rel] = "__install__"
            if dry_run:
                return
            shared = runtime.data_dir() / "wallpaper" / "backup" / "data.unity3d"
            shared.parent.mkdir(parents=True, exist_ok=True)
            if not shared.is_file():
                shutil.copy2(src, shared)
            return
        fam = family_for(rec.op)
        if self.baselines.has(rec.card, fam, rel):
            # 这张卡这一族已经存过基线：不再复制，撤销时统一从基线写回
            rec.modified[rel] = BASELINE
            return
        self.baselines.touch(rec.card, fam, Path(src), rel, dry_run)
        rec.modified[rel] = BASELINE

    def backup_path(self, rec: Record, rel: str) -> Path:
        return self.backups / rec.id / rec.modified[rel]

    def restore_source(self, rec: Record, rel: str):
        """撤销时这张文件从哪还原。

        三种来源：
        - `__install__` 大文件：共享备份一份
        - `__baseline__`：基线层（新版记录），按 卡+族 定位，与记录条数无关
        - 其余：`backups/<记录id>/`（老版本留下的记录，继续认）
        返回 None 表示基线记的是「原本不存在」，调用方应删掉这个文件。
        """
        if rel.startswith("__install__/"):
            return runtime.data_dir() / "wallpaper" / "backup" / "data.unity3d"
        marker = rec.modified.get(rel)
        if marker == BASELINE:
            fam = family_for(rec.op)
            if self.baselines.absent(rec.card, fam, rel):
                return None
            p = self.baselines.path_of(rec.card, fam, rel)
            if p is not None:
                return p
            # 基线丢了（手动清过 data）：给一个不存在的路径，让调用方报
            # "backup missing" 而不是误判成「原本不存在」把文件删掉
            return self.baselines.expected_path(rec.card, fam, rel)
        return self.backups / rec.id / marker
