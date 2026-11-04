"""用户输入 -> 卡 id / 文件路径. CLI 和 TUI 共用

懒人会直接打名字, 或者把文件拖进命令行 (带引号). 这里都吃下去.
"""
import os
from pathlib import Path

from mdstudio.core.cards import fold
from mdstudio.errors import Ambiguous, CardNotFound, MDStudioError
from mdstudio.i18n import tr

# want: 只在"能当 donor 的"里挑. 同名几个 id 里一般只有一个真有 cut-in, 这种不算歧义
WANT = ("cutin", "runfx")


def _is_id(t: str) -> bool:
    # 坑: "²".isdigit() 是 True 但 int("²") 炸. 只认 ASCII 数字
    return t.isascii() and t.isdigit()


def needs_names(text) -> bool:
    return text is not None and not _is_id(str(text).strip().strip('"'))


def card(db, text, inv=None, want=None) -> int:
    t = str(text).strip().strip('"').strip("'")
    if _is_id(t):
        return int(t)
    if not t:
        raise CardNotFound(tr("empty card"))
    if db is None:
        raise MDStudioError(tr("card names aren't loaded, use the card ID"))
    hits = [c for c, _ in db.search(t, limit=500)]
    if not hits:
        raise CardNotFound(tr('no card named like "%s"') % t)
    fq = fold(t)
    exact = [c for c in hits if fold(db.name(c)) == fq]
    cands = exact or hits
    if want in WANT and inv is not None:
        table = inv.cutin if want == "cutin" else inv.runfx
        ok = [c for c in cands if table.get(c) in ("game", "yours")]
        cands = ok or cands
    # 拼错兜底找到的, 哪怕只有一个也要问 (要改的是游戏文件, 不能猜)
    if len(cands) == 1 and not getattr(db, "last_fuzzy", False):
        return cands[0]
    raise Ambiguous(t, cands[:30])


def path(text) -> Path:
    """拖进 cmd 的文件会带引号, 还可能有首尾空格"""
    if text is None:
        raise MDStudioError(tr("no file given"))
    t = str(text).strip().strip('"').strip("'").strip()
    if not t:
        raise MDStudioError(tr("no file given"))
    return Path(os.path.expandvars(os.path.expanduser(t)))
