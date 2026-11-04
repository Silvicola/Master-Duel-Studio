# -*- coding: utf-8 -*-
"""打便携版: Release 发布 + 收集后端与随包资源 + 启动器 + zip.

用法: python tools/build_portable.py [--skip-publish] [--no-zip] [-o 输出目录名]
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
TOOLS_SKIP = ("build_portable.py", "make_icon.py", "write_launcher.py", "assets", "gen_*.py")
# 输出目录名跟随版本号 (core/mdstudio/__init__.py 的 __version__): MDStudio-beta1-20261103。
# 仍可用 -o 覆盖: 资源管理器按路径缓存 exe 图标, 同路径下文件换了也还是显示旧的,
# 想看新图标就换个目录名, 不用去动系统图标缓存。
import re as _re

_m = _re.search(r'__version__ = "([^"]+)"',
                (ROOT / "core" / "mdstudio" / "__init__.py").read_text(encoding="utf-8"))
OUT_NAME = f"MDStudio-{_m.group(1) if _m else 'unknown'}"
if "-o" in sys.argv:
    OUT_NAME = sys.argv[sys.argv.index("-o") + 1]
DIST = ROOT / "dist" / OUT_NAME
ZIP = ROOT / "dist" / f"{OUT_NAME}-便携版.zip"
# dotnet 从 PATH 找, 或环境变量 DOTNET 指定. 不写死安装路径: 开源后别人的 SDK 装在哪都不一样
DOTNET = os.environ.get("DOTNET") or shutil.which("dotnet") or "dotnet"

skip_publish = "--skip-publish" in sys.argv
no_zip = "--no-zip" in sys.argv

# 本地的东西不该被清掉: .pyenv 装一次要几分钟, data/config 是用户数据
KEEP_LOCAL = {".pyenv", "data", "config", "python"}


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
            and any((p / n).exists() for n in ("config", "data", "python"))]
    if not sibs:
        return
    # 目录名带 ISO 日期后缀, 按名字取最大 = 最近发的那一版
    src = max(sibs, key=lambda p: p.name)
    # python\ 是整份随包运行时(约 376MB), 少了这里构建会直接停下, 所以也要继承
    for name in ("config", "data", "python"):
        s, d = src / name, DIST / name
        if s.is_dir() and not d.exists():
            print(f"继承上一份 {name}/ <- {src.name}")
            shutil.copytree(s, d)


adopt_local_state()

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
    shutil.copytree(TOOLS_SRC, DIST / "tools", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", *TOOLS_SKIP))

# ---------------------------------------------------------------- 4b. 抠像模型
# 引擎 matting_model_path() 的查找顺序: 环境变量 > ~/.u2net > 程序目录/tools/matting。
# 不打进包的话, 对方第一次用 AI 抠像就要现下 176MB, 那就不叫解压即用了。
print("收集抠像模型 (tools/matting) …")
MODEL_NAME = "isnet-anime.onnx"
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
matting_dir = DIST / "tools" / "matting"
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
print("写启动器…")
run([sys.executable, str(ROOT / "tools" / "write_launcher.py"), str(DIST)])

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
    # 用户数据与本地环境不该进包: data/config 是运行时生成的, .pyenv 是本地装的
    skip_dirs = {"data", "config", ".pyenv", "__pycache__"}
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(DIST):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                full = Path(root) / f
                zf.write(full, full.relative_to(DIST.parent).as_posix())
    print(f"写出 {ZIP}  {ZIP.stat().st_size / 1024 / 1024:.0f} MB")
