"""卡输入框: 打名字出下拉 (包含匹配), 也能直接打 id. 替掉原来的纯数字 QSpinBox, 没人记得住卡 id"""
import re

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import QCompleter, QLineEdit

from mdstudio import pick
from mdstudio.errors import Ambiguous, MDStudioError
from mdstudio.i18n import tr

_TAIL = re.compile(r"·\s*(\d+)\s*$")      # "Monster Reborn  · 4842"


class CardPicker(QLineEdit):
    def __init__(self, value=0, what="card", parent=None):
        super().__init__(parent)
        self.what = what
        self.db = None
        self._model = QStringListModel(self)
        c = QCompleter(self._model, self)
        c.setCaseSensitivity(Qt.CaseInsensitive)
        c.setFilterMode(Qt.MatchContains)
        c.setMaxVisibleItems(15)
        self.setCompleter(c)
        self.setPlaceholderText(tr("type a %s name or ID…") % tr(what))
        self.setMinimumWidth(320)
        if value:
            self.setValue(value)

    def set_names(self, db, ids=None):
        """ids = 只列这些 (比如 donor 只列真有 cut-in 的), None = 全部"""
        self.db = db
        ids = sorted(ids if ids is not None else db.names, key=lambda c: (db.name(c).lower(), c))
        self._model.setStringList([f"{db.name(c)}  · {c}" for c in ids if db.name(c)])
        t = self.text().strip()
        if t.isdigit():                    # 名字加载前填的 id, 补上名字
            self.setValue(int(t))

    def setValue(self, cid):
        cid = int(cid)
        nm = self.db.name(cid) if self.db else ""
        self.setText(f"{nm}  · {cid}" if nm else str(cid))

    def peek(self):
        """不弹错不猜: 已经选定 (带 · id 尾巴或纯数字) 就给 id, 否则 None. 给自动联动用"""
        t = self.text().strip()
        m = _TAIL.search(t)
        if m:
            return int(m.group(1))
        return int(t) if t.isascii() and t.isdigit() else None

    def value(self) -> int:
        t = self.text().strip()
        m = _TAIL.search(t)
        if m:
            return int(m.group(1))
        if not t:
            raise MDStudioError(tr("pick a %s first") % tr(self.what))
        if self.db is None and not (t.isascii() and t.isdigit()):
            raise MDStudioError(tr("card names aren't loaded (no game folder yet, or still loading). "
                                  "Wait a moment, or type the card ID."))
        try:
            cid = pick.card(self.db, t)
        except Ambiguous as e:
            if len(e.candidates) == 1:      # 拼错兜底只找到一张: 问一下, 别直接改
                c = e.candidates[0]
                raise MDStudioError(tr('No card named "%s". Did you mean %s (%s)? Pick it from the list.')
                                   % (t, self.db.name(c), c))
            raise MDStudioError(tr('"%s" matches several cards (%s…). Pick one from the list.')
                               % (t, ", ".join(map(str, e.candidates[:8]))))
        self.setValue(cid)
        return cid
