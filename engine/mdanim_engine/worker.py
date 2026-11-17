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
        fn = ACTIONS[op]
        # 资源下载这类动作在选游戏目录之前就要能跑，不能强求游戏装好
        if getattr(fn, "needs_ctx", True):
            ctx = Ctx.open(args.pop("game_dir", None), dry_run=dry_run)
        else:
            ctx = None
        # 写入类动作统一按动态索引放行。索引解不开、卡未上线、卡型表外时
        # 一律拒绝，避免拿上一版数据或猜出来的卡框写进游戏。
        if ctx is not None and args.get("card"):
            from mdstudio.core.cardindex import CardIndex
            idx = CardIndex.ensure(ctx.game)
            st = idx.status(int(args["card"]))
            if not idx.ok:
                raise MDStudioError("卡数据无法解析，已禁用写入：" + idx.error)
            if st["unreleased"]:
                raise MDStudioError("这张卡还没上线，已锁定")
            if st["unknown_subtype"]:
                raise MDStudioError("游戏出现未知卡型 %s，卡框未确认，已拒绝写入"
                                    % st["subtype"])
        rec = fn(ctx, **args)
        emit({"ev": "result", "ok": True, "data": _summary(rec, op)})
    except MDStudioError as e:
        emit({"ev": "result", "ok": False, "kind": type(e).__name__, "message": str(e)})
    except Exception as e:
        # 界面只显示一行原因。完整 traceback 走日志通道进历史页，别塞进弹窗：
        # 下载失败这类异常栈能有一千多字符，弹窗会被撑出屏幕，按钮点不到。
        tb = traceback.format_exc()
        emit_log(tb, "ERROR")
        emit({"ev": "result", "ok": False, "kind": type(e).__name__,
              "message": f"{type(e).__name__}: {e}".strip()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
