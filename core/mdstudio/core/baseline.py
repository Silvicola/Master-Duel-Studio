"""基线层: 一张卡的某一类改动只留一份「动手之前的样子」

以前每条记录各自备份一份被改的文件。同一张卡反复生成时，第二份备份的内容是
第一次的输出 —— 而撤销是按时间倒序还原，后写的会盖掉先写的，所以那一份永远
读不到。唯一被真正用到的是最早那份（游戏原版），其余纯占磁盘（一个召唤动画
的贴图约 238MB）。

现在改成：第一次碰某个文件时，把盘上当时的样子存进 `baselines/<卡>/<族>/`。
之后再改同一个文件，基线已经在了就不再复制。复原直接从这里写回，于是「复原」
永远等于「回游戏原版」，与记录堆了几层无关。

manifest 里记「有内容」和「原本不存在」两种状态。原本不存在的，复原时要删掉 ——
不记这一项就分不清「写回旧内容」和「这个文件本来就没有」。这条对无动画卡是必须的：
它们整套 `P<卡>` 都是新建的，基线里没有对应内容，撤销时只能靠这个标记决定删除。
"""
import json
import logging
import shutil
from pathlib import Path

from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")

# 记录里 modified[rel] 的值：内容在基线层，不在 backups/<记录id>/
BASELINE = "__baseline__"


def family_for(op: str) -> str:
    """这份改动属于哪一族，决定基线存在哪个子目录。

    `pack.art`（整合包装的卡图）与自己做的 `overframe.set` 是同一样东西，
    必须归到同一个族，否则先装包再自己改会各自存一份基线，复原只回一半。
    """
    a, _, b = op.partition(".")
    fam = b if a == "pack" else a
    return "overframe" if fam == "art" else fam


class Baselines:
    """`<账号目录>/baselines/<卡号>/<族>/` —— 与 journal 同级，跟着账号走

    多账号各有一份游戏文件副本，基线也必须分开，否则拿 A 号的原版去覆盖 B 号。
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    # ---- 路径 ----
    def _dir(self, card: int, fam: str) -> Path:
        return self.folder / str(int(card)) / fam

    def _manifest(self, card: int, fam: str) -> Path:
        return self._dir(card, fam) / "manifest.json"

    def _files(self, card: int, fam: str) -> Path:
        return self._dir(card, fam) / "files"

    @staticmethod
    def _flat(rel: str) -> str:
        return rel.replace("/", "_")

    # ---- 读 ----
    def load(self, card: int, fam: str) -> dict:
        try:
            raw = json.loads(self._manifest(card, fam).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, card: int, fam: str, m: dict) -> None:
        f = self._manifest(card, fam)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning(tr("baseline manifest not saved for %s: %s"), card, e)

    def has(self, card: int, fam: str, rel: str) -> bool:
        return rel in self.load(card, fam)

    def forget(self, card: int, fam: str, rels) -> None:
        """从 manifest 里丢掉几条记录。旧 Ef 回收时用：那些文件是新建的（记的
        absent），记录已经删了，manifest 里留着就是永远对不上的死条目。
        """
        m = self.load(card, fam)
        keys = set(rels)
        out = {k: v for k, v in m.items() if k not in keys}
        if len(out) != len(m):
            self._save(card, fam, out)

    def absent(self, card: int, fam: str, rel: str) -> bool:
        """这份文件在我们动手之前本来不存在（复原时该删掉）"""
        return self.load(card, fam).get(rel) is False

    def path_of(self, card: int, fam: str, rel: str):
        """基线里存的内容在哪；没有就给 None（保证不会返回一个不存在的路径）"""
        if not self.has(card, fam, rel):
            return None
        p = self._files(card, fam) / self._flat(rel)
        return p if p.is_file() else None

    def expected_path(self, card: int, fam: str, rel: str) -> Path:
        """基线内容本该在哪。文件不在这个路径仍然存在，调用方自己判存在性。"""
        return self._files(card, fam) / self._flat(rel)

    # ---- 写 ----
    def touch(self, card: int, fam: str, src: Path, rel: str, dry_run=False) -> bool:
        """第一次碰这个文件时记下它当时的样子。已经记过就什么都不做。

        返回是否新建了一条基线（用于日志）。src 不存在 = 记「原本不存在」，
        这种情况也必须落一条，否则以后分不清该删还是该写回。
        """
        m = self.load(card, fam)
        if rel in m:
            return False
        src = Path(src)
        if not src.is_file():
            m[rel] = False
            if not dry_run:
                self._save(card, fam, m)
            return True
        if dry_run:
            return True
        d = self._files(card, fam)
        try:
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d / self._flat(rel))
        except OSError as e:
            # 复制失败就不要登记，宁可下次再试：登记了却拿不到内容，复原时会
            # 以为「有原版」然后写回失败，卡在半成品状态
            log.warning(tr("baseline copy failed for %s, retry on next write: %s"), rel, e)
            return False
        m[rel] = True
        self._save(card, fam, m)
        log.info(tr("baseline %s <- %s"), rel, self._dir(card, fam))
        return True

    # ---- 删 ----
    def drop(self, card: int, fam: str) -> None:
        shutil.rmtree(self._dir(card, fam), ignore_errors=True)

    def drop_card(self, card: int) -> None:
        shutil.rmtree(self.folder / str(int(card)), ignore_errors=True)

    def cards(self) -> list:
        """有基线的卡号（设置页显示占用、手动清理时用）"""
        try:
            return sorted(int(p.name) for p in self.folder.iterdir()
                          if p.is_dir() and p.name.isdigit())
        except (OSError, ValueError):
            return []

    def size(self) -> int:
        total = 0
        for p in self.folder.rglob("*") if self.folder.is_dir() else []:
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    # ---- 草稿槽：超框「接着调」的素材 ----
    # 游戏文件走上面的基线；超框那套背景图 / 主体图 / 滑块参数不在游戏目录里，
    # 是程序自己的编辑状态（data/overframe/<账号>/edits/）。它每改一次覆盖一次，
    # 以前撤销就一把清空，用户调好的参数再也拿不回来。
    #
    # 这里给它一个滚动槽：第一次改这张卡时存一份（= 原始态），之后每次改之前
    # 把当前那份移到 `prev`。磁盘固定两份，不随改多少次增长。
    def _draft(self, card: int, fam: str) -> Path:
        return self._dir(card, fam) / "draft"

    def draft(self, card: int, fam: str):
        """当前存着的那份编辑状态；没有给 None"""
        d = self._draft(card, fam)
        return d if (d / "state.json").is_file() else None

    def save_draft(self, card: int, fam: str, files: dict, state: dict, dry_run=False) -> bool:
        """把一份编辑状态存进草稿槽。已经存过就挪到 prev，槽位固定两份。

        files: {槽名: 源文件路径}；state: 其余参数（缩放、偏移、效果类型等）。
        槽位满了就整体替换 prev，更早的那份不留 —— 复原只需要「原始态」，
        调参过程中能退回一步就够了。
        """
        if dry_run:
            return True
        d = self._draft(card, fam)
        prev = self._dir(card, fam) / "draft_prev"
        try:
            if (d / "state.json").is_file():
                if prev.exists():
                    shutil.rmtree(prev, ignore_errors=True)
                d.rename(prev)
            d.mkdir(parents=True, exist_ok=True)
            for slot, src in files.items():
                p = Path(src) if src else None
                if p is not None and p.is_file():
                    shutil.copy2(p, d / f"{slot}{p.suffix.lower() or '.png'}")
            (d / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.warning(tr("over-frame draft not saved for %s: %s"), card, e)
            return False
        return True