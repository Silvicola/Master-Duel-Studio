"""找游戏目录 + 当前生效的 LocalData group"""
import hashlib
import os
import re
import subprocess
from pathlib import Path

from mdstudio.core.crc import get_crc
from mdstudio.errors import GameNotFound, GameRunning
from mdstudio.i18n import tr

GAME_DIR_NAME = "Yu-Gi-Oh!  Master Duel"  # 注意: 两个空格, 官方就这么起的
EXE = "masterduel.exe"
APPID = 1449850                            # Steam 上的 Master Duel


def _steam_libraries():
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(Path(winreg.QueryValueEx(k, name)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    roots.append(Path(r"C:\Program Files (x86)\Steam"))

    libs = []
    for r in roots:
        libs.append(r)
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            libs += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', text)]
    out = []
    for lib in libs:
        if lib not in out:
            out.append(lib)
    return out


def _from_manifest(lib: Path):
    """Steam 的应用清单里直接写了安装目录名, 比猜固定文件夹名可靠

    appmanifest_1449850.acf 里的 installdir 才是真的安装目录, 用户改过名字也认.
    """
    acf = lib / "steamapps" / f"appmanifest_{APPID}.acf"
    if not acf.is_file():
        return None
    try:
        text = acf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"installdir"\s+"([^"]+)"', text)
    return lib / "steamapps" / "common" / m.group(1) if m else None


def _from_uninstall():
    """卸载信息里的 InstallLocation. 有的机器这里是空的, 只当补充来源"""
    try:
        import winreg
        key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App {APPID}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            loc = winreg.QueryValueEx(k, "InstallLocation")[0]
        return Path(loc) if loc else None
    except (ImportError, OSError):
        return None


def _scan_drives():
    """最后兜底: 各盘符下常见的 Steam 库位置

    只探固定几个路径, 不做全盘遍历 (那会把启动拖成几十秒).
    """
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:/")
        try:
            if not root.exists():
                continue
        except OSError:
            continue
        for rel in ("SteamLibrary", "Steam", "Games/Steam", "Program Files (x86)/Steam"):
            yield root / rel


def _looks_like_game(p) -> bool:
    """必须真有 LocalData/<account>/0000, 光有同名文件夹不算"""
    ld = Path(p) / "LocalData"
    return ld.is_dir() and any(ld.glob("*/0000"))


_DETECTED = None   # 进程内缓存: 探测要读注册表、翻 Steam 清单、摸各盘符,
                   # 设置页反复进出不该每次都重扫一遍


def detect_game_dir(override=None, force: bool = False) -> Path:
    """按可靠程度找游戏目录

    顺序: 参数 > 环境变量 > Steam 应用清单 > 猜固定文件夹名 > 卸载信息 > 扫盘.
    全部失败才报错, 界面上再让用户手选.

    结果会缓存到进程结束：第一次可能要读注册表、解析 vdf、探盘符，
    之后的调用直接返回。用户点「重新检测」时传 force=True 强制重扫。
    """
    global _DETECTED
    if override:
        return Path(override)
    if not force and _DETECTED is not None and _looks_like_game(_DETECTED):
        return _DETECTED

    libs = _steam_libraries()
    cands = [os.environ.get("MDSTUDIO_GAME")]
    cands += [_from_manifest(lib) for lib in libs]                 # 最准
    cands += [lib / "steamapps" / "common" / GAME_DIR_NAME for lib in libs]
    cands.append(_from_uninstall())
    cands += [r / "steamapps" / "common" / GAME_DIR_NAME for r in _scan_drives()]

    for c in cands:
        if c and _looks_like_game(c):
            _DETECTED = Path(c)
            return _DETECTED
    raise GameNotFound(tr("Master Duel not found. Pass --game <folder> or set MDSTUDIO_GAME."))


def list_accounts(game_dir) -> list[str]:
    """游戏目录下所有账号 (= LocalData/<account>/0000 里的 <account>)

    实测: 账号 ID 在 LocalData 与 LocalSave 下成对出现且各有 0000 子目录,
    所以它是账号标识, 不是游戏版本残留.
    """
    ld = Path(game_dir) / "LocalData"
    if not ld.is_dir():
        return []
    return sorted(p.parent.name for p in ld.glob("*/0000") if p.is_dir())


class Game:
    def __init__(self, game_dir, group: str = None):
        self.dir = Path(game_dir)
        self.localdata = self.dir / "LocalData"
        self.root = self._root_for(group)

    def _root_for(self, group: str = None) -> Path:
        roots = [p for p in self.localdata.glob("*/0000") if p.is_dir()]
        if not roots:
            raise GameNotFound(tr("no LocalData group under %s") % self.localdata)

        if group:
            for r in roots:
                if r.parent.name == group:
                    return r
            raise GameNotFound(tr("account %s not found under %s") % (group, self.localdata))

        # 没指定账号时的兜底：按目录名取第一个。
        #
        # 以前是「挑子目录文件最多的那个」，但那个判断既贵又不可靠：
        # 要遍历每个账号的 257 个子目录（合计 40000+ 文件，实测 91ms），
        # 而本机两个账号的文件数**完全相同**（各 40889 个），挑出来纯看遍历顺序，
        # 每次还可能不一样。Game 每开一个 Ctx 就建一次，设置页一次打开建好几个，
        # 光这一项就白等几百毫秒。
        #
        # 账号就是 LocalData/<账号>/0000 的目录名，取哪个都能用；用户一旦在设置页
        # 选过就存在 settings 里，走上面 group 那条路，根本不进这里。所以这里只要
        # **稳定**即可，sorted 取第一个既确定又零成本。
        return sorted(roots, key=lambda r: r.parent.name)[0]

    @classmethod
    def detect(cls, override=None, group: str = None) -> "Game":
        return cls(detect_game_dir(override), group)

    def accounts(self) -> list[str]:
        return list_accounts(self.dir)

    @property
    def group(self) -> str:
        return self.root.parent.name

    @property
    def install_id(self) -> str:
        # journal 按安装路径分开, 一台机器多个安装也不串
        return hashlib.sha1(str(self.dir.resolve()).lower().encode()).hexdigest()[:12]

    def path_for(self, h: str) -> Path:
        return self.root / h[:2] / h

    def find(self, h: str):
        p = self.path_for(h)
        return p if p.is_file() else None

    def find_logical(self, logical: str):
        return self.find(get_crc(logical))

    def rel(self, p) -> str:
        p = Path(p)
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            pass
        # 安装根目录下的文件（如 data.unity3d）用哨兵锚定，让 journal/undo 也能管到它
        try:
            return "__install__/" + p.relative_to(self.dir).as_posix()
        except ValueError:
            return p.as_posix()

    def abs(self, rel: str) -> Path:
        if rel.startswith("__install__/"):
            return self.dir / rel[len("__install__/"):]
        return self.root / rel


def is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        # 坑: text=True 会按 Python 的 locale 解码 tasklist 的输出, 中文系统上
        # tasklist 实际吐 cp936, 对不上时 subprocess 的读取线程直接炸掉, stdout 变 None.
        # 只关心进程名在不在, 按字节比, 不解码, 任何 codepage 下都成立
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXE}", "/NH"],
                             capture_output=True, creationflags=0x08000000).stdout
    except OSError:
        return False
    return EXE.lower().encode() in (out or b"").lower()


def ensure_closed():
    # 游戏开着时写 LocalData = 文件被占用 or 写了也不生效, 直接拦
    if is_running():
        raise GameRunning(tr("Master Duel is running. Close it completely first."))
