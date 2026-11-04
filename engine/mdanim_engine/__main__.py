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

    backend = Backend()
    jobs = JobRunner(backend.invalidate)
    protocol.event("ready", pid=__import__("os").getpid())

    from .actions import ACTIONS

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
