"""进度: op 只管报, 前端 (GUI / TUI / CLI) 自己挂 listener 决定怎么显示. 没人听 = 只写 log

step(text)        一个阶段开始, 进 log
tick(text, i, n)  阶段里第 i/n 个, 不进 log (一帧一行太吵)
"""
import logging
import threading

log = logging.getLogger("mdstudio")
_lock = threading.Lock()
_listeners = []


def listen(fn):
    """fn(text, i, n); i/n 为 None = 不知道多久. 返回取消函数"""
    with _lock:
        _listeners.append(fn)

    def off():
        with _lock:
            if fn in _listeners:
                _listeners.remove(fn)
    return off


def _emit(text, i, n):
    with _lock:
        fns = list(_listeners)
    for fn in fns:
        try:
            fn(text, i, n)
        except Exception:   # 显示坏了不能把正在写游戏文件的任务带崩
            pass


def step(text, *args):
    text = text % args if args else text
    log.info(text)
    _emit(text, None, None)


def tick(text, i, n):
    _emit(text, i, n)
