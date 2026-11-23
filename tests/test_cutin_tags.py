"""动画页 2026-11-15 定稿：标签只描述现状，来历交给悬停；生成后必须先复原。

三件事：
1. 自改动画不管原本有没有官方，标签都写「自改动画」（不再有「自改动画★」）；
2. 悬停按来历分三种：来自无动画 / 来自官方动画 / 来自官方动画★；
3. 「有生成类记录就拦」不能把换贴图（reskin）一起拦掉，那是生成之后的正常下一步。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "engine"))

from mdstudio.errors import MDStudioError  # noqa: E402
from mdstudio.ops import cutin as C  # noqa: E402
from mdanim_engine.backend import Backend  # noqa: E402


def _journal(*ops):
    """假的 journal：`for_card(card, only)` 要按 only 过滤，跟真的一样"""
    def for_card(card, only=None):
        return [SimpleNamespace(op=op, card=card)
                for op in ops if only is None or op in only]
    return SimpleNamespace(for_card=for_card)


# ---------------------------------------------------------------- 拦的边界

def test_generated_flags_video_and_spine():
    assert C.generated(SimpleNamespace(journal=_journal("cutin.video")), 14952)
    assert C.generated(SimpleNamespace(journal=_journal("cutin.spine")), 14952)


def test_generated_ignores_reskin():
    """换贴图是生成之后才做的，不能跟着一起拦"""
    ctx = SimpleNamespace(journal=_journal("cutin.reskin"))
    assert not C.generated(ctx, 14952)


def test_generated_ignores_other_cards():
    ctx = SimpleNamespace(journal=SimpleNamespace(for_card=lambda card, only=None: []))
    assert not C.generated(ctx, 14952)


def test_video_apply_refuses_without_restore():
    """引擎层也得拦：界面的拦截挡不住命令行"""
    from mdstudio.ops.video import cutin_flip as F

    ctx = SimpleNamespace(journal=_journal("cutin.video"), dry_run=False)
    try:
        F.apply(ctx, 14952, "whatever")
    except MDStudioError as e:
        assert "复原" in str(e) or "Restore it first" in str(e)
    else:
        raise AssertionError("重复生成没有被拦住")


# ---------------------------------------------------------------- 来历三档

def _fill(rows, official=(), borrowed=None):
    class Fake:
        def _official_origin(self, ctx):
            return set(official)

        def _borrowed_variants(self, ctx):
            return dict(borrowed or {})

        def _fx_origin_kind(self, ctx):
            return {}

        def _borrowed_fx(self, ctx):
            return {}

    Backend._fill_origin(Fake(), None, rows)
    return {r["id"]: r.get("cutin_origin") for r in rows}


def test_origin_official():
    assert _fill([{"id": 100, "cutin": "yours"}], official={100})[100] == "official"


def test_origin_borrowed_variant():
    got = _fill([{"id": 200, "cutin": "yours"}], borrowed={200: 300})
    assert got[200] == "shared"


def test_origin_none():
    assert _fill([{"id": 5, "cutin": "yours"}])[5] == ""


def test_origin_only_for_self_rows():
    """官方卡、借用卡的悬停另有内容，不该带自改的来历字段"""
    got = _fill([{"id": 300, "cutin": "game"}, {"id": 400, "cutin": "shared"}])
    assert got[300] is None and got[400] is None

# ---------------------------------------------------------------- 底盘页数

def _fake_cat(mrk, q, scale, pages, style="underscore"):
    """只够 chassis_pages / scale_dirs 用的假清单

    style: underscore = P<卡>_2（3881 那种）；plain = P<卡>2（15640 那种）
    """
    tex = f"{C.base(mrk)}/{q}/{scale}/P{mrk}"
    prefab = C.prefab(mrk, q)
    second = (f"{tex}_2" if style == "underscore" else f"{tex}2") if pages == 2 else None
    names = {prefab, tex} | ({second} if second else set())

    class Cat:
        def __contains__(self, n):
            return n in names

        def deps(self, n):
            return sorted(names - {prefab}) if n == prefab else []

        def prefab_candidates(self, base, quality, mrk):
            p = f"{base}/{quality}/P{int(mrk)}"
            return [p] if p in names else []

        def names(self):
            return sorted(names)

    return Cat()


def test_chassis_pages_reads_one_or_two():
    """#21382 这类官方动画只有一页贴图，双页卡是两页"""
    assert C.chassis_pages(_fake_cat(11016, C.HD, "1", 2), 11016, C.HD) == 2
    assert C.chassis_pages(_fake_cat(21382, C.HD, "0.63", 1), 21382, C.HD) == 1


def test_chassis_pages_reads_both_page_namings():
    """官方页名两种写法都要认：P3881_2 带下划线，P156402 不带。

    以前只认带下划线的，仪式 15640 / 同调 7698 / 连接 13508 被判成单页卡，
    要 2 页时静默换成了 3881 的底盘（2026-11-16 实测）。
    """
    assert C.chassis_pages(_fake_cat(7698, C.HD, "1", 2, "plain"), 7698, C.HD) == 2
    assert C.page_tails(_fake_cat(7698, C.HD, "1", 2, "plain"), 7698, C.HD) == ["P7698", "P76982"]


def test_page_rename_map_only_covers_plain_naming():
    """带下划线的 retag_str 自己改得到，不用进表；不带下划线的必须进，否则克隆后留旧名"""
    assert C.page_rename_map(_fake_cat(7698, C.HD, "1", 2, "plain"), 7698, 888888) == {
        "76982": "888888_2"}
    assert C.page_rename_map(_fake_cat(3881, C.HD, "1", 2), 3881, 888888) == {}


def test_page_tails_ignores_same_prefixed_other_card():
    """P19172 是卡 19172 的第一页，不能当成卡 1917 的第二页。

    全库有 169 个 P<基号>2 形态，其中 52 个「基号+2」是真卡号。只能按目录归属判。
    """
    p1917 = f"{C.base(1917)}/HighEnd_HD/0.9/P1917"
    p1917_2 = p1917 + "2"                      # 卡 1917 的第二页，也叫 P19172
    p19172 = f"{C.base(19172)}/HighEnd_HD/0.86/P19172"   # 卡 19172 的第一页，同名

    class Cat:
        names_ = {C.prefab(1917, C.HD), p1917, p1917_2,
                  C.prefab(19172, C.HD), p19172}

        def __contains__(self, n):
            return n in self.names_

        def deps(self, n):
            return sorted(self.names_ - {n}) if n in self.names_ else []

        def prefab_candidates(self, base, quality, mrk):
            p = f"{base}/{quality}/P{int(mrk)}"
            return [p] if p in self.names_ else []

        def names(self):
            return sorted(self.names_)

    cat = Cat()
    assert C.page_tails(cat, 1917, C.HD) == ["P1917", "P19172"]
    assert C.page_tails(cat, 19172, C.HD) == ["P19172"]


def test_donor_with_pages_returns_zero_when_nothing_fits():
    assert C.donor_with_pages(None, _fake_cat(11358, C.HD, "1", 1), 5, 2, [C.HD]) == 0
