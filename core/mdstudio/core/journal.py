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
from pathlib import Path

from mdstudio import runtime
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr


RENAMED_OPS = {"cutin.mdpro3": "cutin.spine"}


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

    def backup(self, rec: Record, src: Path, rel: str, dry_run=False):
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
        name = rel.replace("/", "_")
        rec.modified[rel] = name
        if dry_run:
            return
        d = self.backups / rec.id
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, d / name)

    def backup_path(self, rec: Record, rel: str) -> Path:
        return self.backups / rec.id / rec.modified[rel]

    def restore_source(self, rec: Record, rel: str) -> Path:
        """撤销时这张文件从哪还原：普通文件用本记录的备份，安装根大文件用共享备份。"""
        if rel.startswith("__install__/"):
            return runtime.data_dir() / "wallpaper" / "backup" / "data.unity3d"
        return self.backups / rec.id / rec.modified[rel]
