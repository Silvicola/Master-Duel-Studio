"""atlas 页与页之间必须有空行。

Spine 的 atlas 解析器靠空行切页。缺了空行，第二个页名会被当成上一页的一个 region，
第二页的所有 region 全部作废：图集看着是两页、贴图也在，但骨架只认得到第一页的帧，
运行时算出的动画时长只有第一页那部分，窗口又按完整时长走，游戏里就完整播两遍
（2026-11-18 实测 #22116：120 帧分 64+56，只播 64 帧 = 1.07 秒）。
官方 atlas 也是这么写的（P3881_2.png 前面有空行），照抄就对了。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from mdstudio.ops.video.cutin_flip import gen_atlas


def _pages():
    return [
        ("P1.png", (2048, 2048), [("frame_0", 0, 0, 512, 512), ("frame_1", 512, 0, 512, 512)]),
        ("P1_2.png", (2048, 2048), [("frame_2", 0, 0, 512, 512)]),
    ]


def test_two_pages_have_blank_line_between():
    lines = gen_atlas(_pages()).split("\n")
    assert lines[0] == "P1.png", "第一页前面不能有空行"
    idx = lines.index("P1_2.png")
    assert lines[idx - 1] == "", "第二页页名前必须有一个空行, 否则整页 region 作废"


def test_single_page_has_no_leading_blank():
    lines = gen_atlas(_pages()[:1]).split("\n")
    assert lines[0] == "P1.png"


def test_regions_keep_order_across_pages():
    text = gen_atlas(_pages())
    lines = [ln for ln in text.split("\n") if ln and ":" not in ln and not ln.endswith(".png")]
    assert lines == ["frame_0", "frame_1", "frame_2"]
