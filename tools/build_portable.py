# -*- coding: utf-8 -*-
"""打便携版: Release 发布 + 收集后端与随包资源 + 启动器 + zip.

用法: python tools/build_portable.py [--skip-publish] [--no-zip] [-o 输出目录名]

随包 Python(约 364MB) 不在本脚本里生成, 从 ..\\Env\\python 取主副本.
删掉 dist 目录也能自动补回; 要装新依赖请装在 Env\\python 上, 构建会同步过去.
换位置用环境变量 MDSTUDIO_ENV 指定.
"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# 路径全部相对脚本自身推导, 不写死绝对路径: 整个工程目录改名/搬家后照样能构建
ROOT = Path(__file__).resolve().parents[1]          # tools/ 的上一级 = 程序工程目录
PROJ = ROOT / "src" / "MDStudio"
# 引擎源码与随包资源都在本工程内（2026-11-02 从旧的 MDCutinStudio 并过来）
# 注意：不能放 src/ 下。Windows 路径不区分大小写，src/mdstudio 会和 src/MDStudio 撞成同一个目录
CORE_SRC = ROOT / "core" / "mdstudio"
TOOLS_SRC = ROOT / "tools"
# tools/ 里除了随包资源，还有构建脚本本身，别把它们塞进包里
# gen_*.py 是素材生成器，依赖作者本地的参考图，跑不起来也用不上
TOOLS_SKIP = ("build_portable.py", "make_icon.py", "assets", "gen_*.py")
# 输出目录名跟随版本号 (core/mdstudio/__init__.py 的 __version__): MDStudio-v-1.0.0-beta_202611161353。
# 版本号写法 v-主.次.修订-beta_年月日时分 (2026-11-16 起精确到分钟, 12 位)。
# 打包时按当前时刻自动写回 __init__.py, 不用手工改版本号。
# 仍可用 -o 覆盖目录名: 资源管理器按路径缓存 exe 图标, 同路径下文件换了也还是显示旧的,
# 想看新图标就换个目录名, 不用去动系统图标缓存。
import re as _re
from datetime import datetime as _dt

_INIT = ROOT / "core" / "mdstudio" / "__init__.py"
_no_stamp = "--no-stamp" in sys.argv   # 调试用: 保留当前版本号, 不按时间刷新
# 版本号约定（2026-11-18 用户定）：v-主.次.修订-release_年月日时分
#   主：不常动；次：有功能新增时 +1（如加预览功能 -> 1.1.0）；修订：修 bug 时 +1（如 1.0.1）
# 时间戳每次打包自动刷新；主.次.修订不动，除非显式 --bump=minor|patch|major。
_bump = ""
for _arg in sys.argv:
    if _arg.startswith("--bump="):
        _bump = _arg.split("=", 1)[1].strip().lower()
if _bump not in ("", "patch", "minor", "major"):
    raise SystemExit("--bump 只接受 patch / minor / major")
if not _no_stamp:
    _now = _dt.now()
    _stamp = _now.strftime("%Y%m%d%H%M")
    _build = _now.strftime("%Y.%m.%d")
    _src = _INIT.read_text(encoding="utf-8")
    _vm = _re.search(r'__version__\s*=\s*"v-(\d+)\.(\d+)\.(\d+)', _src)
    _maj, _min, _pat = (int(x) for x in _vm.groups()) if _vm else (1, 0, 0)
    if _bump == "major":
        _maj, _min, _pat = _maj + 1, 0, 0
    elif _bump == "minor":
        _min, _pat = _min + 1, 0
    elif _bump == "patch":
        _pat += 1
    _ver = f"v-{_maj}.{_min}.{_pat}-release_{_stamp}"
    # 2026-11-17 起进入正式版，版本号后缀固定 release_（以前的 beta_ 不再写回）
    _src = _re.sub(r'(__version__\s*=\s*")v-[^"]+(")',
                   lambda m: m.group(1) + _ver + m.group(2), _src, count=1)
    _src = _re.sub(r'(__build__\s*=\s*")[^"]*(")',
                   lambda m: m.group(1) + _build + m.group(2), _src, count=1)
    _INIT.write_text(_src, encoding="utf-8")
    print(f"版本号自动写回: {_ver}  build {_build}", flush=True)

_m = _re.search(r'__version__ = "([^"]+)"', _INIT.read_text(encoding="utf-8"))
OUT_NAME = f"MDStudio-{_m.group(1) if _m else 'unknown'}"
if "-o" in sys.argv:
    i = sys.argv.index("-o")
    if i + 1 >= len(sys.argv):
        raise SystemExit("-o 后面要跟输出目录名, 例如: -o MDStudio-test")
    OUT_NAME = sys.argv[i + 1]
DIST = ROOT / "dist" / OUT_NAME
ZIP = ROOT / "dist" / f"{OUT_NAME}-便携版.zip"
# dotnet 从 PATH 找, 或环境变量 DOTNET 指定. 不写死安装路径: 开源后别人的 SDK 装在哪都不一样
DOTNET = os.environ.get("DOTNET") or shutil.which("dotnet") or "dotnet"
# 随包 Python(整份 base Python + 依赖, 约 364MB)的主副本放在工程外面:
# dist\ 是构建产物, 用户习惯每次测试都删掉重解压, 放里面迟早丢; Env\ 不在那个目录里
ENV_DIR = Path(os.environ.get("MDSTUDIO_ENV") or (ROOT.parent / "Env"))
PY_MASTER = ENV_DIR / "python"

skip_publish = "--skip-publish" in sys.argv
no_zip = "--no-zip" in sys.argv
# 便携版默认**不带**额外组件（抠像模型 168MB / 超分 39MB / Spine 转换器 11MB）:
# 这三样由用户在设置页按需下载, 下到程序目录的 tools/ 下同一个位置。
# 想要一解压就全都能用的离线包, 加 --full。
slim = "--full" not in sys.argv
# 默认只留 zip：同名目录是打包过程的中间产物，用户自己解压就行。
# 调试要直接跑那个目录时加 --keep-dir。
keep_dir = "--keep-dir" in sys.argv

# 本地的东西不该被清掉: .pyenv 装一次要几分钟, data/config 是用户数据,
# tools/ 里还存着用户在设置页按需下载的组件（抠像模型 168MB / 超分 39MB / 转换器 11MB）——
# 不保留的话每打一次包就得重下一遍。源文件照旧覆盖过来（copytree dirs_exist_ok）。
KEEP_LOCAL = {".pyenv", "data", "config", "python", "tools"}


def run(args, cwd=None):
    print(">", " ".join(str(a) for a in args), flush=True)
    p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout[-3000:])
        print(p.stderr[-3000:])
        raise SystemExit(f"失败: {args[1] if len(args) > 1 else args}")
    return p.stdout


def dir_size_mb(p: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1024 / 1024


# ---------------------------------------------------------------- 1. 发布
PUB = PROJ / "bin" / "Release" / "net10.0-windows10.0.26100.0" / "win-x64" / "publish"

if not skip_publish:
    # SatelliteResourceLanguages: 只留简体中文, 去掉八十多个语言资源目录
    run([DOTNET, "publish", "-c", "Release", "-r", "win-x64",
         "--self-contained", "true",
         "-p:WindowsAppSDKSelfContained=true",
         "-p:SatelliteResourceLanguages=zh-CN"], cwd=PROJ)

if not (PUB / "MDStudio.exe").is_file():
    raise SystemExit(f"没找到发布产物: {PUB}")

# 发布成功之后再清目标目录, 免得 publish 失败把上一份好的产物也弄没了.
# 但留着本地环境与用户数据
if DIST.exists():
    for item in list(DIST.iterdir()):
        if item.name in KEEP_LOCAL:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
else:
    DIST.mkdir(parents=True, exist_ok=True)


def adopt_local_state():
    """换输出目录名时, 把上一份产物的 config/ 与 data/ 继承过来.

    这两样都住在构建目录里: config 是游戏目录与账号, data 是历史记录与卡名缓存.
    上面的 KEEP_LOCAL 只保护「同一个目录内」的, 改了版本号目录名一新, 新包就等于
    第一次运行 —— 2026-11-04 就因此丢了设置, 连接怪兽的箭头画不出来.
    只在目标缺失时复制, 已经在用的目录不会被旧内容盖掉.
    """
    sibs = [p for p in (ROOT / "dist").glob("MDStudio-*")
            if p.is_dir() and p.resolve() != DIST.resolve()
            and any((p / n).exists() for n in ("config", "data"))]
    if not sibs:
        return
    # 目录名带 ISO 日期后缀, 按名字取最大 = 最近发的那一版
    src = max(sibs, key=lambda p: p.name)
    for name in ("config", "data"):
        s, d = src / name, DIST / name
        if s.is_dir() and not d.exists():
            print(f"继承上一份 {name}/ <- {src.name}")
            shutil.copytree(s, d)


def tree_fp(p: Path):
    """目录指纹: 文件数 + 总字节. 用来判断两份 Python 是否已经一样."""
    n = total = 0
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            try:
                total += os.path.getsize(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return (n, total)


def ensure_python():
    """把随包 Python 放进构建目录. 脚本只搬不改, 自己生成不了这一块.

    Env\\python 是唯一源: 要往随包环境里装东西(新依赖、补 DLL)就装在它上面,
    构建时整个复制进 dist. 反过来往 dist 里装没用, 下次构建会被覆盖.
    dist 是纯输出目录, 随时会被用户清空, 绝不能从里面取 Python 当源 ——
    既不沿用构建目录那份, 也不从上一份产物复制 (2026-11-16 用户定, 口径要唯一).
    """
    dst = DIST / "python"
    if not (PY_MASTER / "python.exe").is_file():
        raise SystemExit(
            f"缺少随包 Python 主副本: {PY_MASTER / 'python.exe'}\n"
            f"把一份装好依赖的完整 Python 3.13 放到 {PY_MASTER} 再跑, "
            f"或用环境变量 MDSTUDIO_ENV 指向别的位置.\n"
            f"(dist 里的 python 不作为源, 它随时可能被清空.)")
    if (dst / "python.exe").is_file() and tree_fp(dst) == tree_fp(PY_MASTER):
        return
    # 换代时先整目录改名再删: rename 是目录级操作, 不会被逐文件删盘保护拦下,
    # 也就不会出现"旧的删一半, 新的还没来, dist 只剩半个"的半残状态。
    # 暂存名放在 DIST 外面, 就算最后删不掉也不会被压进 zip。
    trash = ROOT / "dist" / "python_old_tmp"
    if trash.exists():
        shutil.rmtree(trash, ignore_errors=True)
    if dst.exists():
        try:
            dst.rename(trash)
        except OSError:
            shutil.rmtree(dst, ignore_errors=True)
    print(f"搬随包 Python <- {PY_MASTER}  {dir_size_mb(PY_MASTER):.0f} MB, 稍等…")
    shutil.copytree(PY_MASTER, dst)
    if trash.exists():
        shutil.rmtree(trash, ignore_errors=True)   # 新的已就位, 旧的删不掉也不碍事


adopt_local_state()
ensure_python()

# ---------------------------------------------------------------- 2. 收主程序
# publish 目录里的东西一个都不能漏: .pri 是 XAML 资源索引, 少了 MainWindow.xaml 就加载失败
print("收集主程序…")
for item in PUB.iterdir():
    if item.is_dir():
        shutil.copytree(item, DIST / item.name, dirs_exist_ok=True)
    else:
        shutil.copy2(item, DIST / item.name)

# ---------------------------------------------------------------- 3. 引擎与后端
print("收集引擎脚本…")
engine_src = ROOT / "engine"
shutil.copytree(engine_src, DIST / "engine", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

print("收集后端源码 (core/mdstudio) …")
shutil.copytree(CORE_SRC, DIST / "core" / "mdstudio", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

# ---------------------------------------------------------------- 4. 随包资源
# mdstudio 的 runtime.bundle_root() 在 core/mdstudio/ 的上两级 = 程序目录,
# 所以 tools/ 要跟 core/ 平级, 引擎才找得到
if TOOLS_SRC.is_dir():
    print("收集随包资源 (tools) …")
    skip = list(TOOLS_SKIP) + (["matting", "realesrgan", "spineconv"] if slim else [])
    shutil.copytree(TOOLS_SRC, DIST / "tools", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", *skip))

# ---------------------------------------------------------------- 4b. 抠像模型
# 引擎 matting_model_path() 只认: 环境变量 > 程序目录/tools/matting（2026-11-16 起
# 不再退回 ~/.u2net，避免"设置页说没装、生成却跑得动"）。
# 所以瘦身包里不塞模型: 用户首次用到时在设置页下，落在同一个位置。
# 加 --full 才带；那时允许从本机缓存里找一份，纯粹是省一次重复下载。
print("收集抠像模型 (tools/matting) …")
MODEL_NAME = "isnet-anime.onnx"
matting_dir = DIST / "tools" / "matting"
if slim:
    print("   默认不带额外组件：跳过（用户首次用到时下载）")
    matting_dir.mkdir(parents=True, exist_ok=True)
    src_model = None
else:
    model_candidates = []
    env_model = os.environ.get("MDSTUDIO_MATTING_MODEL")
    if env_model:
        model_candidates.append(Path(env_model))
    model_candidates += [Path.home() / ".u2net" / MODEL_NAME,
                         TOOLS_SRC / "matting" / MODEL_NAME,
                         DIST / "tools" / "matting" / MODEL_NAME]
    src_model = next((p for p in model_candidates if p and p.is_file()), None)
    if src_model is None:
        raise SystemExit(f"找不到 {MODEL_NAME}。先跑一次抠像让它自己下, 或者手动放进 {TOOLS_SRC / 'matting'}")
    matting_dir.mkdir(parents=True, exist_ok=True)
    dst_model = matting_dir / MODEL_NAME
    if not dst_model.is_file() or dst_model.stat().st_size != src_model.stat().st_size:
        shutil.copy2(src_model, dst_model)
    print(f"   {MODEL_NAME}  {dst_model.stat().st_size / 1024 / 1024:.0f} MB  <- {src_model}")

# ---------------------------------------------------------------- 4c. VC 运行库
# python\ 里有 vcruntime140*.dll (Python 自带的), 但没有 msvcp140*.dll。
# onnxruntime / UnityPyBoost / astc_encoder 都要 msvcp140, 本机系统装了 VC++ 运行库
# 看不出问题, 换台没装过的机器 import 直接失败。随包补上, 1MB 买个保险。
print("补随包 VC 运行库 (python) …")
PY_DIR = DIST / "python"
if not (PY_DIR / "python.exe").is_file():
    raise SystemExit(f"缺少随包 Python: {PY_DIR}\\python.exe")
for dll in ("msvcp140.dll", "msvcp140_1.dll"):
    src = Path(r"C:\Windows\System32") / dll
    dst = PY_DIR / dll
    if dst.is_file():
        continue
    if src.is_file():
        shutil.copy2(src, dst)
        print(f"   + {dll}")
    else:
        print(f"   ! 系统里没有 {dll}, 跳过 (对方可能装了 VC++ 运行库)")

# ---------------------------------------------------------------- 5. 启动器
# 2026-11-21 去掉「启动.cmd / start.cmd / 使用说明.txt」三件套：
# 包里的 python\ 与 exe 都已自包含，启动器唯一有效动作就是 start MDStudio.exe，
# 剩下几条分支（找系统 Python、建 .pyenv、pip 装依赖）既用不上，也和「不需要联网」矛盾。
# 说明文字过期更快，删掉比维护省事，用户双击 MDStudio.exe 即可。

# ---------------------------------------------------------------- 6. 清掉不该带的
for junk in ("MDStudio.pdb",):
    f = DIST / junk
    if f.exists():
        f.unlink()

# ---------------------------------------------------------------- 7. 汇总
size = dir_size_mb(DIST)
print(f"\n便携版目录: {DIST}")
print(f"总大小: {size:.0f} MB")
for sub in sorted(DIST.iterdir()):
    if sub.is_dir():
        print(f"   {sub.name + '/':<16} {dir_size_mb(sub):8.0f} MB")
    else:
        print(f"   {sub.name:<16} {sub.stat().st_size / 1024:8.0f} KB")

# ---------------------------------------------------------------- 8. 打包
if not no_zip:
    print("\n压缩…")
    if ZIP.exists():
        ZIP.unlink()
    # data/config/.pyenv/.download 只会出现在程序根。按名字全局剪会误伤树里
    # 任何位置的同名目录（比如以后 core 下加个 data/），所以只在顶层剪它们，
    # 更深的目录只剪编译缓存。
    root_only = {"data", "config", ".pyenv", ".download"}
    # tools/ 现在跨版本保留（见 KEEP_LOCAL），里面可能有用户下过的组件。
    # 瘦身包不能把这几样再压进去，否则包会从 260MB 涨到 480MB, 也违背「按需下载」。
    # --full 时反过来: 那个包就是要带上它们。
    fat_dirs = set() if not slim else {"matting", "realesrgan", "spineconv"}
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(DIST):
            if os.path.normcase(root) == os.path.normcase(str(DIST)):
                dirs[:] = [d for d in dirs
                           if d not in root_only and d != "__pycache__"]
            elif os.path.normcase(root) == os.path.normcase(str(DIST / "tools")):
                dirs[:] = [d for d in dirs if d != "__pycache__" and d not in fat_dirs]
            else:
                dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                full = Path(root) / f
                zf.write(full, full.relative_to(DIST.parent).as_posix())
    print(f"写出 {ZIP}  {ZIP.stat().st_size / 1024 / 1024:.0f} MB")

if not keep_dir and not no_zip and DIST.is_dir():
    shutil.rmtree(DIST, ignore_errors=True)
    if DIST.is_dir():
        # 这里不要用 ⚠ 之类的符号：cp936 控制台编码失败会让整个打包以 1 退出，
        # 现象是「文件都写好了但脚本报失败」（2026-11-24 实测）。
        print(f"[!] 解压目录没删干净（可能被杀软或沙箱占用），可手动删除：{DIST}")
    else:
        print("已删除解压目录（要保留请加 --keep-dir）")
