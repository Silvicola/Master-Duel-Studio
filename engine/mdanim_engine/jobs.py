"""动作任务: 起独立的 Python 子进程跑, 主线程继续读 stdin, 取消随时能进来.

不用 multiprocessing: `python -m 包` 起的进程里 spawn 子进程会撞上引导问题,
子进程拿不到入口函数. 直接用 subprocess + 一行 JSON 递任务, 和对外协议同一套思路.

两个槽位:
  主槽    写游戏文件的正经动作, 互斥执行, busy() 挡住后来的.
  轻任务槽  不碰游戏文件的后台活 (组件下载), 单独一个槽位不与主槽互斥——
          下载几分钟不该把"生成超框"这类正经动作挡在门外 (2026-11-11 用户实测).
          两者写的目录无交集 (data/tools vs 游戏目录+data/journal), 并行安全.
"""
import json
import os
import subprocess
import sys
import threading

from . import protocol


class _Slot:
    """一个任务槽: 子进程 + 归属信息 + 取消标志."""
    __slots__ = ("proc", "rid", "op", "canceled")

    def __init__(self, proc, rid, op):
        self.proc = proc
        self.rid = rid
        self.op = op
        self.canceled = False


class JobRunner:
    def __init__(self, on_finish):
        self._on_finish = on_finish       # 动作跑完后让 backend 作废缓存
        self._main = None                 # 主槽 (_Slot)
        self._light = None                # 轻任务槽 (_Slot)

    def busy(self) -> bool:
        return self._main is not None

    def light_busy(self) -> bool:
        return self._light is not None

    # ------------------------------------------------------------------ 启动
    def _spawn(self, op: str, args: dict, dry_run: bool):
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
        return proc

    def start(self, rid, op: str, args: dict, dry_run: bool) -> None:
        if self._main is not None:        # 分发层已挡, 这里兜底
            protocol.fail(rid, "Busy", "还有动作在跑")
            return
        self._main = _Slot(self._spawn(op, args, dry_run), rid, op)
        protocol.event("job", state="started", id=rid, op=op)
        threading.Thread(target=self._pump_err, args=(self._main.proc,), daemon=True).start()
        threading.Thread(target=self._drain,
                         args=(self._main, False), daemon=True).start()

    def start_light(self, rid, op: str, args: dict, dry_run: bool = False) -> None:
        """把 op 放进轻任务槽: 不占主槽, 正经动作照常接."""
        if self._light is not None:
            protocol.fail(rid, "Busy", "已有下载在跑, 等它结束或先取消")
            return
        self._light = _Slot(self._spawn(op, args, dry_run), rid, op)
        protocol.event("job", state="started", id=rid, op=op)
        threading.Thread(target=self._pump_err, args=(self._light.proc,), daemon=True).start()
        threading.Thread(target=self._drain,
                         args=(self._light, True), daemon=True).start()

    # ------------------------------------------------------------------ 取消
    def cancel(self) -> bool:
        """杀掉子进程. 返回 False = 当时两个槽都没有任务在跑"""
        hit = False
        for slot in (self._main, self._light):
            if slot is not None:
                slot.canceled = True
                slot.proc.terminate()
                hit = True
        return hit

    # ------------------------------------------------------------------ 收尾
    def _finish(self, slot) -> None:
        if self._main is slot:
            self._main = None
        if self._light is slot:
            self._light = None
        self._on_finish()                 # 轻任务也调: 组件装好后 needs 查询要新鲜

    @staticmethod
    def _pump_err(proc) -> None:
        """必须一直读 stderr: 不读的话管道写满会把子进程堵死"""
        for line in proc.stderr:
            if sys.stderr is not None:
                sys.stderr.write("[worker] " + line)

    def _drain(self, slot, light: bool) -> None:
        proc, rid, op = slot.proc, slot.rid, slot.op
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
                # 带上 op: 主槽和轻任务槽是两个子进程, 进度事件共用一个通道。
                # 不带的话设置页下载时, 别的页面报的阶段文字会打到下载那一条上
                # (2026-11-12 用户实测: 下组件时切去写超框, 下载行文字被换掉)。
                protocol.event("progress", text=msg.get("text", ""),
                               i=msg.get("i", -1), n=msg.get("n", 0), job=op)
            elif kind == "result":
                result = msg
                break

        proc.wait()

        if slot.canceled:
            protocol.fail(rid, "Canceled", "已取消")
        elif result is None:
            protocol.fail(rid, "Crashed", "动作进程没有返回结果就退出了")
        elif result.get("ok"):
            protocol.ok(rid, result.get("data"))
        else:
            protocol.fail(rid, result.get("kind", "Error"), result.get("message", ""))

        protocol.event("job", state="done", id=rid, op=op)
        self._finish(slot)
