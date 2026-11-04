# -*- coding: utf-8 -*-
"""写便携版的 启动.cmd 和 使用说明.txt.

.cmd 必须 CRLF + UTF-8 无 BOM, chcp 65001 不能删(不然中文提示是乱码).
"""
import os
import sys
from pathlib import Path

# 兜底路径也相对推导, 免得工程目录改名后指向一个不存在的地方
_ROOT = Path(__file__).resolve().parents[1]
DIST = sys.argv[1] if len(sys.argv) > 1 else str(_ROOT / "dist" / "MDStudio-v3")

BAT = r"""@echo off
rem chcp must come right after @echo off. Lines before it are read with the
rem system code page (936), so UTF-8 Chinese in a comment gets mis-decoded and
rem some bytes are treated as shell metacharacters. That breaks the rem line
rem and cmd reports "is not recognized as an internal or external command".
rem Once the code page is 65001 the following lines are read as UTF-8.
rem NOTE: comments stay ASCII on purpose. Chinese only in top-level echo lines.
chcp 65001 >nul
rem Double-click to start MDStudio.
rem Dependencies live in the program's own python\ folder, the system Python is untouched.
rem Structure: no parenthesised blocks, error branches use goto labels.
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0python\python.exe"
if exist "%PY%" goto launch
set "PY=%~dp0.pyenv\Scripts\python.exe"
if exist "%PY%" goto checkdeps
goto makenv

:checkdeps
"%PY%" -c "import UnityPy, PIL, numpy, prompt_toolkit, cv2, onnxruntime, imageio_ffmpeg" 2>nul
if errorlevel 1 goto installdeps
goto launch

:makenv
rem 找一个能用的系统 Python。优先 py 启动器, 它的 PATH 最干净
set "SYS="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if not errorlevel 1 set "SYS=py -3"
if not "%SYS%"=="" goto makenv2
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if not errorlevel 1 set "SYS=python"
if not "%SYS%"=="" goto makenv2
goto nopython

:makenv2
echo 首次运行: 正在创建独立环境 .pyenv ...
%SYS% -m venv "%~dp0.pyenv"
if errorlevel 1 goto venvfail
if not exist "%~dp0.pyenv\Scripts\python.exe" goto venvfail
set "PY=%~dp0.pyenv\Scripts\python.exe"

:installdeps
echo 首次运行: 正在安装依赖 (装进 .pyenv, 要几分钟)...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install UnityPy Pillow numpy prompt_toolkit opencv-python-headless onnxruntime imageio-ffmpeg
if errorlevel 1 goto installfail

:launch
echo 正在启动 MDStudio ...
start "" "%~dp0MDStudio.exe"
exit /b 0

:nopython
echo 找不到 Python 3.10 或更新版本。
echo 请从 python.org 安装 Python, 安装时勾选 "Add python.exe to PATH"。
pause
exit /b 1

:venvfail
echo 创建 .pyenv 失败。请检查这个目录能不能写。
pause
exit /b 1

:installfail
echo 安装失败。请向上滚动查看原因, 常见是没有网络。
pause
exit /b 1
"""

README = r"""Master Duel Studio（游戏王大师决斗 召唤动画 / 发动特效 工具）
=================================================================

怎么用
------
解压后直接双击 MDStudio.exe（或「启动.cmd」）即可，不需要装 Python，也不需要联网。

需要什么
--------
· Windows 10 1809 或更新（不需要装 Python，不需要联网，不需要管理员权限）
· 游戏本体（Steam 版 Master Duel）
· 包里的 python\、tools\、core\、engine\ 都是程序的一部分，不要删

首次运行之后
------------
· Python 与依赖都在随包的 python\ 里，不动你的系统 Python
· 记录与备份写在程序目录的 data\ 下
· 设置写在程序目录的 config\ 下
· 想整个搬到别的盘或 U 盘，把这个文件夹整个拷走就行

已经随包的东西
--------------
· Python 3.13 与全部依赖（UnityPy / Pillow / numpy / opencv / onnxruntime / ffmpeg）
· AI 抠像模型 tools\matting\isnet-anime.onnx（176MB），选「AI 抠像」可直接用
· VC++ 运行库 msvcp140*.dll，没装过 Visual C++ 运行库的机器也能跑
· 超分模型与 tools\realesrgan\realesrgan-ncnn-vulkan.exe（超分要显卡支持 Vulkan，
  没有的话选「不超分」即可，其它功能不受影响）

常见问题
--------
· 双击没反应：把「启动.cmd」里的报错截图发来。
· 换卡提示找不到游戏：在设置页手动选择 Master Duel 的安装目录。
· 抠像很慢：是 CPU 推理，第一次会花几秒加载模型，之后每帧几百毫秒。
"""


def write(path, text, crlf=True, bom=False):
    data = text.replace("\r\n", "\n")
    if crlf:
        data = data.replace("\n", "\r\n")
    raw = data.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    with open(path, "wb") as f:
        f.write(raw)
    print("写出", os.path.basename(path), len(raw), "字节", "CRLF" if crlf else "LF")


os.makedirs(DIST, exist_ok=True)
write(os.path.join(DIST, "启动.cmd"), BAT)
# cmd 从命令行接中文文件名时按 ANSI 代码页解析, 必然乱码(cmd /c "启动.cmd" 会失败),
# 所以再放一份英文名副本, 脚本和文档用 start.cmd, 用户双击还是用中文那个。
write(os.path.join(DIST, "start.cmd"), BAT)
write(os.path.join(DIST, "使用说明.txt"), README, crlf=False)
