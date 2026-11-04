"""所有写盘都过 Ctx: dry-run / 原子写 / 日志 一处搞定"""
import logging
import os
import shutil
import stat
import time
from pathlib import Path

from mdstudio.core.game import Game, ensure_closed
from mdstudio.core.journal import Journal
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")


def atomic_write(path: Path, data: bytes):
    # 先写临时文件再 replace, 写一半崩了也不会留个坏 bundle
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".mdstudio-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    # 游戏自己的文件有时带只读属性, os.replace 覆盖会报 WinError 5, 先放开
    if path.exists():
        try:
            mode = path.stat().st_mode
            if not mode & stat.S_IWRITE:
                os.chmod(path, mode | stat.S_IWRITE)
        except OSError:
            pass
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # 杀毒/索引服务偶尔短暂占着刚写的文件
            if i == 7:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.25 * (i + 1))


class Ctx:
    def __init__(self, game: Game, dry_run: bool = False, journal: Journal = None, check_running: bool = True):
        self.game = game
        self.dry_run = dry_run
        self.journal = journal or Journal.for_game(game)
        self.check_running = check_running

    @classmethod
    def open(cls, game_dir=None, dry_run=False):
        """优先用设置里选好的目录与账号; 命令行显式给了目录就以命令行为准"""
        from mdstudio import settings
        saved = settings.load()
        gdir = game_dir or saved.get("game_dir") or None
        acc = saved.get("account")
        group = acc if acc and settings.valid_account(gdir, acc) else None
        return cls(Game.detect(gdir, group), dry_run=dry_run)

    def guard(self):
        if self.check_running and not self.dry_run:
            ensure_closed()

    def write(self, path, data: bytes):
        if self.dry_run:
            log.info(tr("[dry-run] write %s (%s bytes)"), self._show(path), len(data))
            return
        atomic_write(Path(path), data)

    def copy(self, src, dst):
        if self.dry_run:
            log.info(tr("[dry-run] copy %s -> %s"), self._show(src), self._show(dst))
            return
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def delete(self, path):
        if self.dry_run:
            log.info(tr("[dry-run] delete %s"), self._show(path))
            return
        Path(path).unlink(missing_ok=True)

    def _show(self, p):
        try:
            return self.game.rel(p)
        except ValueError:
            return str(p)
