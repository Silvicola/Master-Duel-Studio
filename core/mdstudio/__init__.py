"""mdstudio - Master Duel cut-in / run-effect toolkit."""

__version__ = "beta1-20261106"   # 测试版第 1 版 + 年月日
__build__ = "2026.11.06"         # 打包日期


def ensure_std_streams():
    """保证 sys.stdout / sys.stderr 存在, 有控制台时是空操作.

    坑 (2026-10-22, 用户实测): GUI 走 pythonw 启动, 没有控制台; 重活又丢给
    multiprocessing spawn 子进程跑, 子进程里 sys.stdout/stderr 直接是 None.
    第三方库不知道这事, 会照常往上写: rembg 下载分割模型时 tqdm 进度条
    fp.write 一个 None.write, AttributeError 当场炸, AI 抠像模式在 GUI 下必崩.
    这里补一个写进黑洞的流, 谁都不遭殃.
    """
    import os
    import sys
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w"))
