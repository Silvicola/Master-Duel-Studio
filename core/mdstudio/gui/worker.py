"""后台线程 + 把 logging 转成 Qt 信号 (跨线程只能走信号)"""
import logging
import traceback

from PySide6.QtCore import QObject, QRunnable, Signal

from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr


class _Sink(QObject):
    line = Signal(str, int)
    progress = Signal(str, int, int)   # text, i, n; i = -1 = 只是换阶段, 不知道多久


class LogBridge(logging.Handler):
    # 不直接多继承 QObject: Handler.emit 跟 Qt 那套容易打架, 包一层
    def __init__(self):
        super().__init__()
        self.sink = _Sink()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.sink.line.emit(self.format(record), record.levelno)
        except RuntimeError:   # 窗口关了 sink 已经没了
            pass

    def on_progress(self, text, i, n):
        # mdstudio.progress 的 listener, 在工作线程里被调, 只能走信号
        try:
            self.sink.progress.emit(text, -1 if i is None else int(i), int(n or 0))
        except RuntimeError:
            pass


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


def _child(fn, spec, args, kw, q):
    """子进程里跑 op. 坑: 压 BC7 贴图的 C 扩展不放 GIL, 放线程里跑窗口会'无响应'几分钟,
    所以重活放子进程, log / 进度 / 结果走 Queue 回来"""
    # 坑: spawn 子进程里 stdout/stderr 是 None (pythonw 没有控制台), 第三方库
    # (rembg 下载模型的 tqdm 进度条) 直接往上写会当场炸, 先补上流再干活
    from mdstudio import ensure_std_streams
    ensure_std_streams()
    from mdstudio import progress
    from mdstudio.core.context import Ctx
    from mdstudio.core.game import Game
    from mdstudio.core.journal import Journal

    class _Q(logging.Handler):
        def emit(self, record):
            q.put(("log", self.format(record), record.levelno))

    lg = logging.getLogger("mdstudio")
    h = _Q()
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    progress.listen(lambda text, i, n: q.put(("prog", text, -1 if i is None else int(i), int(n or 0))))
    try:
        game_dir, group, dry, jfolder, check = spec
        ctx = Ctx(Game(game_dir, group or None), dry_run=dry,
                  journal=Journal(jfolder), check_running=check)
        q.put(("done", fn(ctx, *args, **kw)))
    except MDStudioError as e:
        q.put(("fail", str(e)))
    except Exception as e:
        q.put(("fail", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"))


def picklable(fn) -> bool:
    q = getattr(fn, "__qualname__", "")
    return bool(getattr(fn, "__module__", "")) and fn.__module__ != "__main__" and "<" not in q


class ProcJob(QRunnable):
    """跟 Job 一样的信号, 但 fn 在子进程里跑 (fn 必须是模块级函数). 这个线程只管收 Queue"""

    def __init__(self, fn, ctx, *args, **kw):
        super().__init__()
        self.fn, self.args, self.kw = fn, args, kw
        # 账号必须跟着走: 子进程重建 Game 时不能退回"文件最多"的猜测,
        # 否则会写进另一个账号的目录
        self.spec = (str(ctx.game.dir), ctx.game.group, ctx.dry_run,
                     str(ctx.journal.folder), ctx.check_running)
        self.signals = _Signals()
        self.sink = None            # LogBridge.sink, 用来把子进程的 log / 进度转给窗口
        self.setAutoDelete(False)

    def run(self):
        import multiprocessing as mp
        import queue as _queue
        mpc = mp.get_context("spawn")
        q = mpc.Queue()
        p = mpc.Process(target=_child, args=(self.fn, self.spec, self.args, self.kw, q), daemon=True)
        sig, arg = self.signals.failed, tr("the worker process stopped without a result")
        try:
            p.start()
            while True:
                try:
                    msg = q.get(timeout=0.5)
                except _queue.Empty:
                    if not p.is_alive():
                        break
                    continue
                kind = msg[0]
                if kind == "log" and self.sink:
                    self.sink.line.emit(msg[1], msg[2])
                elif kind == "prog" and self.sink:
                    self.sink.progress.emit(msg[1], msg[2], msg[3])
                elif kind == "done":
                    sig, arg = self.signals.done, msg[1]
                    break
                elif kind == "fail":
                    sig, arg = self.signals.failed, msg[1]
                    break
            p.join(timeout=10)
        except RuntimeError:
            return                   # 窗口关了
        except Exception as e:
            sig, arg = self.signals.failed, f"{type(e).__name__}: {e}"
        try:
            sig.emit(arg)
        except RuntimeError:
            pass


class Job(QRunnable):
    def __init__(self, fn, *args, **kw):
        super().__init__()
        self.fn, self.args, self.kw = fn, args, kw
        self.signals = _Signals()
        # Qt 默认跑完就删 runnable, 排队中的信号会被丢. 生命周期交给 Python (Main._jobs 拿着)
        self.setAutoDelete(False)

    def run(self):
        try:
            sig, arg = self.signals.done, self.fn(*self.args, **self.kw)
        except MDStudioError as e:
            sig, arg = self.signals.failed, str(e)
        except Exception as e:  # 未知错误也别让线程静默死掉
            logging.getLogger("mdstudio").debug(traceback.format_exc())
            sig, arg = self.signals.failed, f"{type(e).__name__}: {e}"
        try:
            sig.emit(arg)
        except RuntimeError:
            pass   # 任务还没跑完窗口就关了, 结果没人要
