"""动作任务: 起独立的 Python 子进程跑, 主线程继续读 stdin, 取消随时能进来.

不用 multiprocessing: `python -m 包` 起的进程里 spawn 子进程会撞上引导问题,
子进程拿不到入口函数. 直接用 subprocess + 一行 JSON 递任务, 和对外协议同一套思路.
"""
import json
import os
import subprocess
import sys
import threading

from . import protocol


class JobRunner:
    def __init__(self, on_finish):
        self._on_finish = on_finish       # 动作跑完后让 backend 作废缓存
        self._proc = None
        self._rid = None
        self._op = None
        self._canceled = False

    def busy(self) -> bool:
        return self._proc is not None

    # ------------------------------------------------------------------ 启动
    def start(self, rid, op: str, args: dict, dry_run: bool) -> None:
        spec = json.dumps({"op": op, "args": dict(args), "dry_run": dry_run},
                          ensure_ascii=False)
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "mdanim_engine.worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=dict(os.environ))         # PYTHONPATH 已经在环境里, 继承即可
        try:
            proc.stdin.write(spec + "\n")
            proc.stdin.flush()
        except OSError:
            pass

        self._proc, self._rid, self._op, self._canceled = proc, rid, op, False
        protocol.event("job", state="started", id=rid, op=op)
        threading.Thread(target=self._pump_err, args=(proc,), daemon=True).start()
        threading.Thread(target=self._drain, args=(proc, rid, op), daemon=True).start()

    # ------------------------------------------------------------------ 取消
    def cancel(self) -> bool:
        """杀掉子进程. 返回 False = 当时没有任务在跑"""
        if self._proc is None:
            return False
        self._canceled = True
        self._proc.terminate()
        return True

    # ------------------------------------------------------------------ 收尾
    def _finish(self) -> None:
        self._proc = None
        self._rid = None
        self._op = None
        self._on_finish()

    @staticmethod
    def _pump_err(proc) -> None:
        """必须一直读 stderr: 不读的话管道写满会把子进程堵死"""
        for line in proc.stderr:
            if sys.stderr is not None:
                sys.stderr.write("[worker] " + line)

    def _drain(self, proc, rid, op: str) -> None:
        result = None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = msg.get("ev")
            if kind == "log":
                protocol.event("log", level=msg.get("level", "INFO"),
                               text=msg.get("text", ""))
            elif kind == "progress":
                protocol.event("progress", text=msg.get("text", ""),
                               i=msg.get("i", -1), n=msg.get("n", 0))
            elif kind == "result":
                result = msg
                break

        proc.wait()

        if self._canceled:
            protocol.fail(rid, "Canceled", "已取消")
        elif result is None:
            protocol.fail(rid, "Crashed", "动作进程没有返回结果就退出了")
        elif result.get("ok"):
            protocol.ok(rid, result.get("data"))
        else:
            protocol.fail(rid, result.get("kind", "Error"), result.get("message", ""))

        protocol.event("job", state="done", id=rid, op=op)
        self._finish()
