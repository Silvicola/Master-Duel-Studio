"""stdio JSON Lines 协议.

约定:
  - 一行一条 JSON, stdout 只跑协议, 别的输出一律走 stderr
  - 请求: {"id": 1, "op": "cards.status", "args": {...}}
  - 应答: {"id": 1, "ok": true, "data": ...} / {"id": 1, "ok": false, "error": {...}}
  - 事件: {"ev": "log"|"progress"|"job", ...}, 没有 id
"""
import json
import sys

_out = sys.stdout          # 先抓住真正的 stdout, 之后把它藏起来


def hide_stdout() -> None:
    """把 sys.stdout 指向 stderr.

    协议占了 fd 1, 任何库里的 print / tqdm 都会把 JSON 行冲坏.
    """
    sys.stdout = sys.stderr


def send(obj) -> None:
    _out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _out.flush()


def event(kind: str, **kw) -> None:
    send({"ev": kind, **kw})


def ok(rid, data=None) -> None:
    send({"id": rid, "ok": True, "data": data})


def fail(rid, kind: str, message: str) -> None:
    send({"id": rid, "ok": False, "error": {"kind": kind, "message": message}})
