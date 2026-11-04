"""界面语言: 中文系统默认中文, 其他系统英文; MDSTUDIO_LANG=zh|en 强制覆盖.

英文原文即 msgid: tr("Borrow") 在中文界面返回 "借用", 英文界面原样返回.
译文全在 i18n_zh.ZH 里; 漏翻的词条原样返回英文而不是抛错, 增量翻译时软件始终能跑.
CLI 子命令名/参数名/数据键( op 名, journal 字段, manifest, gate 数据 )不过 tr, 那些不是界面文本.
"""
import os

from mdstudio.i18n_zh import ZH


def _env_lang() -> "str | None":
    """MDSTUDIO_LANG 优先, 兼容 zh-cn / chinese 等写法

    Returns:
        "zh" / "en", 或 None (没设, 或设了不认得的值).
    """
    v = os.environ.get("MDSTUDIO_LANG", "").strip().lower().replace("-", "_")
    if v in ("zh", "zh_cn", "zh_hans", "cn", "chinese"):
        return "zh"
    if v in ("en", "english"):
        return "en"
    return None


def _system_lang() -> str:
    """看系统界面语言: 中文 -> zh, 其余 -> en

    Windows 走 GetUserDefaultUILanguage, 其他平台看 LANG 等环境变量.
    """
    if os.name == "nt":
        try:
            import ctypes
            ui = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if ui in (0x0804, 0x0404):      # zh-CN / zh-TW
                return "zh"
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var, "").lower().startswith("zh"):
            return "zh"
    return "en"


LANG: str = _env_lang() or _system_lang()


def tr(s: str, ctx: str = "") -> str:
    """译一条界面文本.

    Args:
        s: 英文原文, 同时是 i18n_zh.ZH 的 key.
        ctx: 同形异义时的消歧后缀. 比如 badge 状态列的 "yes" (已有) 和
             表格在盘率列的 "yes" (是) 是同一个英文词, 词典里写成 "badge|yes".

    Returns:
        中文译文; 英文界面或漏翻时原样返回, 永不抛异常.
    """
    if LANG != "zh":
        return s
    if ctx:
        v = ZH.get(f"{ctx}|{s}")
        if v is not None:
            return v
    return ZH.get(s, s)
