"""子进程里跑一个动作.

为什么是独立进程: 压 BC7 贴图的 C 扩展不放 GIL, 放引擎进程里会卡死, 取消命令就没人处理.

协议(内部, 只在父子之间):
    stdin  一行 JSON:  {"op": "...", "args": {...}, "dry_run": false}
    stdout 若干行 JSON: {"ev": "log"|"progress"|"result", ...}
"""
import json
import logging
import os
import sys
import traceback


def _summary(rec, op: str) -> dict:
    """只回摘要: Record 里有 catalog 大字典, 没必要整份搬回去

    批量动作(spine.port_all / spine.check)返回的是 dict 或 list, 不是 Record,
    照旧套 Record 那套会把结果整份吞掉, 界面就看不到哪些成功、哪些跳过。
    """
    if rec is None:
        return {"op": op, "card": None, "created": 0, "modified": 0}
    if isinstance(rec, dict):
        return {"op": op, **rec}
    if isinstance(rec, (list, tuple)):
        return {"op": op, "count": len(rec), "items": list(rec)}
    return {
        "op": getattr(rec, "op", op),
        "card": getattr(rec, "card", None),
        "created": len(getattr(rec, "created", None) or []),
        "modified": len(getattr(rec, "modified", None) or {}),
    }


def main() -> int:
    # fd 1 是事件通道. 先复制一份留给自己, 再把 fd 1 指到 stderr,
    # 这样库里的 print / tqdm 走 stderr, 冲不坏事件流
    event_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    events = os.fdopen(event_fd, "w", encoding="utf-8", buffering=1)

    def emit(obj):
        events.write(json.dumps(obj, ensure_ascii=False) + "\n")
        events.flush()

    try:
        spec = json.loads(sys.stdin.readline())
    except Exception as e:
        emit({"ev": "result", "ok": False, "kind": "BadSpec", "message": str(e)})
        return 1

    op = spec.get("op")
    args = spec.get("args") or {}
    dry_run = bool(spec.get("dry_run"))

    from mdstudio import progress
    from mdstudio.core.context import Ctx
    from mdstudio.errors import MDStudioError

    from .actions import ACTIONS

    def emit_log(text, level="INFO"):
        emit({"ev": "log", "level": level, "text": text})

    class _Sink(logging.Handler):
        def emit(self, record):
            try:
                emit_log(self.format(record), record.levelname)
            except Exception:
                pass

    lg = logging.getLogger("mdstudio")
    h = _Sink()
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    lg.setLevel(logging.INFO)

    progress.listen(lambda text, i, n: emit({
        "ev": "progress", "text": text,
        "i": -1 if i is None else int(i), "n": int(n or 0)}))

    try:
        ctx = Ctx.open(args.pop("game_dir", None), dry_run=dry_run)
        rec = ACTIONS[op](ctx, **args)
        emit({"ev": "result", "ok": True, "data": _summary(rec, op)})
    except MDStudioError as e:
        emit({"ev": "result", "ok": False, "kind": type(e).__name__, "message": str(e)})
    except Exception as e:
        emit({"ev": "result", "ok": False, "kind": type(e).__name__,
              "message": f"{e}\n{traceback.format_exc()[-1200:]}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
