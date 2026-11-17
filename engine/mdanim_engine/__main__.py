"""引擎入口: 读 stdin 的 JSON 行, 回 stdout 的 JSON 行.

C# 侧一行一条, 不需要端口, 生命周期跟着界面走.
"""
import json
import logging
import sys

from . import protocol
from .backend import QUERY, Backend
from .jobs import JobRunner


def main() -> int:
    protocol.hide_stdout()
    # 引擎自己的日志走 stderr, 别混进协议
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    # 预热：设置页一进来就会问这几样，而它们首次调用都要付一笔冷启动开销
    # （导入模块、读 config/settings.json），之后各自都是 1ms 以内。
    # 在这里一次付掉，前两条加起来不到 10ms。
    #
    # 索引**不在**这里建。以前在这里同步跑 CardIndex.ensure()，冷启动要 4~11 秒，
    # 而 ready 事件发在它后面，于是引擎一条请求都不收，设置页四个区块全等它建完
    # 才一起「啪」地出现。现在索引留给真正需要它的那条查询，界面按到达先后上屏。
    try:
        from mdstudio import assets, settings  # noqa: F401

        settings.load()
        assets.status()
    except Exception:
        pass

    backend = Backend()
    jobs = JobRunner(backend.invalidate)
    # 让 backend 能看见主任务槽：切账号/换目录要等动作跑完，否则正在跑的子进程
    # 按旧账号解析好的写入路径会和新的设置对不上，记录落到另一个账号里。
    backend.jobs = jobs

    # 主进程这边的进度只来自卡数据索引（别的重活在 worker 子进程里报，走 "progress"）。
    # 单独用 "index" 这个事件类型，界面里两个进度条才不会互相串：动画页订阅的是
    # "progress"，设置页只认 "index"。按文字过滤是不行的，界面语言一变就全废。
    from mdstudio import progress

    progress.listen(lambda text, i, n: protocol.event(
        "index", text=text, i=-1 if i is None else int(i), n=int(n or 0)))

    protocol.event("ready", pid=__import__("os").getpid())

    from .actions import ACTIONS

    # 走轻任务槽的 op: 不写游戏文件、不与正经动作互斥的后台活。
    # 组件下载一跑几分钟, 占主槽会把其他页面全部挡在门外。
    LIGHT_OPS = {"assets.ensure"}

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            protocol.send({"id": None, "ok": False,
                           "error": {"kind": "BadJson", "message": str(e)}})
            continue

        rid = req.get("id")
        op = req.get("op")
        args = req.get("args") or {}

        try:
            if op == "cancel":
                protocol.ok(rid, {"canceled": jobs.cancel()})
            elif op == "shutdown":
                protocol.ok(rid, None)
                return 0
            elif op in QUERY:
                protocol.ok(rid, QUERY[op](backend, **args))
            elif op in LIGHT_OPS:
                if jobs.light_busy():
                    protocol.fail(rid, "Busy", "已有下载在跑, 等它结束或先取消")
                else:
                    jobs.start_light(rid, op, args, bool(args.pop("dry_run", False)))
            elif op in ACTIONS:
                if jobs.busy():
                    protocol.fail(rid, "Busy", "还有动作在跑, 等它结束或先取消")
                else:
                    jobs.start(rid, op, args, bool(args.pop("dry_run", False)))
            else:
                protocol.fail(rid, "UnknownOp", f"不认识的命令: {op}")
        except Exception as e:                     # 一条命令炸了不能带走整个引擎
            protocol.fail(rid, type(e).__name__, str(e))

    return 0


if __name__ == "__main__":
    sys.exit(main())
