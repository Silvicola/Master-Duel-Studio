"""决斗音效的白名单: data.unity3d 里的 SE_DUEL_Resources

游戏只播白名单里登记过的音效。名字不在表里时, 就算文件放在正确的哈希路径上,
游戏也不会去读它, 结果是静音。这一条是 2026-11-13 实测得出的:
同样的 Vorbis 原件放在新名字下没声音, 表里加一条之后立刻有声。

表是一份 XML (LabelSettings/LabelSet), 打包在安装根的 data.unity3d 里。
改一次要把整个 128MB 的包重打一遍, 实测约 7 秒, 所以这个模块不做缓存,
每次操作现读现写; 单次操作只加或删一条。

原包只留一份备份, 放 data_dir()/sound/backup/data.unity3d。因为多条音效共用
同一份包, 撤销某一条不能整包还原, 只能"把那条 label 删掉再重打"。
"""
import json
import re
import shutil
import tempfile
from pathlib import Path

import UnityPy

from mdstudio import runtime
from mdstudio.errors import MDStudioError, NotOurs
from mdstudio.i18n import tr

TABLE = "SE_DUEL_Resources"
FILE = "data.unity3d"
CATEGORY = "SE_DUEL"

# 白名单条目的模板。值和表里现有条目一致, 只换名字
ENTRY = """\t<LabelSet>
\t\t<LabelName>{label}</LabelName>
\t\t<FileName>{label}</FileName>
\t\t<Loop>FALSE</Loop>
\t\t<Volume>{volume}</Volume>
\t\t<CategoryBehavior>STEAL_OLDEST</CategoryBehavior>
\t\t<Priority>10</Priority>
\t\t<CategoryName>{category}</CategoryName>
\t\t<UnityMixerName>SE</UnityMixerName>
\t\t<FadeOutTime>0.1</FadeOutTime>
\t</LabelSet>
"""


def table_path(game) -> Path:
    """安装根的 data.unity3d。找不到就报错, 别静默跳过"""
    p = Path(game.dir) / "masterduel_Data" / FILE
    if not p.is_file():
        raise MDStudioError(tr("%s not found under the game folder") % FILE)
    return p


def backup_path() -> Path:
    d = runtime.data_dir() / "sound" / "backup"
    d.mkdir(parents=True, exist_ok=True)
    return d / FILE


def ensure_backup(game) -> Path:
    """第一次改动前留一份原包。之后一直复用, 不再覆盖"""
    bak = backup_path()
    if not bak.is_file():
        shutil.copy2(table_path(game), bak)
    return bak


def _load(game):
    env = UnityPy.load(table_path(game).read_bytes())
    for o in env.objects:
        if o.type.name != "TextAsset":
            continue
        d = o.read()
        if (getattr(d, "m_Name", "") or "") != TABLE:
            continue
        raw = d.m_Script
        txt = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
        return env, o, d, txt
    raise MDStudioError(tr("%s not found in %s") % (TABLE, FILE))


def labels(game) -> list:
    """白名单里现有的全部名字"""
    _, _, _, txt = _load(game)
    return re.findall(r"<LabelName>([^<]+)</LabelName>", txt)


def has(game, label: str) -> bool:
    return label in set(labels(game))


def _replace(env, obj, data, txt: str) -> bytes:
    data.m_Script = txt
    data.save()
    tmp = Path(tempfile.mkdtemp())
    try:
        env.save(pack="original", out_path=str(tmp))
        made = tmp / FILE
        if not made.is_file():
            outs = [p for p in tmp.rglob("*") if p.is_file()]
            if len(outs) != 1:
                raise MDStudioError(tr("failed to write %s") % FILE)
            made = outs[0]
        return made.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def add(game, label: str, volume: float = 1) -> bool:
    """登记一条。已经在表里就什么都不做, 返回 False"""
    env, obj, data, txt = _load(game)
    if f"<LabelName>{label}</LabelName>" in txt:
        return False
    entry = ENTRY.format(label=label, volume=volume, category=CATEGORY)
    idx = txt.rindex("</LabelSettings>")
    new = txt[:idx] + entry + txt[idx:]
    ensure_backup(game)
    table_path(game).write_bytes(_replace(env, obj, data, new))
    return True


def remove(game, label: str) -> bool:
    """删掉一条。表里没有就报错, 让调用方知道自己记错了"""
    env, obj, data, txt = _load(game)
    pat = re.compile(r"\t*<LabelSet>(?:(?!</LabelSet>).)*?<LabelName>%s</LabelName>"
                     r"(?:(?!</LabelSet>).)*?</LabelSet>\r?\n?" % re.escape(label), re.S)
    new, n = pat.subn("", txt, count=1)
    if not n:
        raise NotOurs(tr("%s is not registered in the sound list") % label)
    ensure_backup(game)
    table_path(game).write_bytes(_replace(env, obj, data, new))
    return True


def restore_original(game) -> bool:
    """整份换回原包。Steam 验证或游戏更新也能达到同样效果"""
    bak = backup_path()
    if not bak.is_file():
        return False
    shutil.copy2(bak, table_path(game))
    return True


def dirty(game) -> bool:
    """包是不是被我们改过（和原包字节不同）"""
    bak = backup_path()
    if not bak.is_file():
        return False
    try:
        return bak.read_bytes() != table_path(game).read_bytes()
    except OSError:
        return False
