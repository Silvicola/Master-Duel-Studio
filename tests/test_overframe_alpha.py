"""超框图的主体透明度：预览保持不透明，写入版把主体压成 alpha=4。

闪卡与镜碎按这个值判断「哪里不画框、不铺箔」。效果框、点亮箭头、顶部那排
（卡名条 / 等级星 / 属性图标）必须保持不透明，否则游戏会在那些地方挖空。
"""
import numpy as np
from PIL import Image

from mdstudio.ops import overframe as OF


def _alpha(path):
    return np.asarray(Image.open(path).convert("RGBA"))[:, :, 3]


def test_preview_keeps_subject_opaque(tmp_path):
    sub = tmp_path / "subject.png"
    Image.new("RGBA", (200, 400), (230, 60, 60, 255)).save(sub)

    out = tmp_path / "preview.png"
    OF.compose(3401, subject=str(sub), out=str(out))

    assert not (_alpha(out) == OF.SUBJECT_MASK_ALPHA).any()


def test_write_version_masks_subject(tmp_path):
    sub = tmp_path / "subject.png"
    Image.new("RGBA", (200, 400), (230, 60, 60, 255)).save(sub)

    out = tmp_path / "write.png"
    OF.compose(3401, subject=str(sub), out=str(out), mask_alpha=OF.SUBJECT_MASK_ALPHA)

    a = _alpha(out)
    assert (a == OF.SUBJECT_MASK_ALPHA).sum() > 10000      # 主体那一大块被压过
    lx, ly, lw, lh = OF.lore_rect(OF.frame_of_card(3401))
    # 效果框统一留不透明：这块必须由面板压住主体
    assert not (a[ly:ly + lh, lx:lx + lw] == OF.SUBJECT_MASK_ALPHA).any()
