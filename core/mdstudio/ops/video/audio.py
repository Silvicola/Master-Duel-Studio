"""视频自带的声音 -> WAV, 给特效用

ffmpeg: 先找 PATH, 没有就用 imageio-ffmpeg 自带的 (pip 装 [video] 就有)
响度: 原声塞进游戏很小声, 照 Phainon 那次的做法 acompressor + loudnorm I=-8
长度: 特效把整段视频压/拉到 duration 秒放, 声音也得跟着变速, 不然对不上
"""
import shutil
import subprocess
from pathlib import Path

from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr
from mdstudio.progress import step


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise MDStudioError(tr("taking the sound out of a video needs ffmpeg. Run:  "
                              "python -m pip install imageio-ffmpeg")) from e


def _run(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, r.stderr


def video_length(src) -> float:
    # ffmpeg -i 不给输出文件会报错退出, 但 stderr 里有 Duration
    _rc, err = _run([ffmpeg_exe(), "-hide_banner", "-i", str(src)])
    for line in err.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            h, m, s = line.split(",")[0].split()[1].split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise MDStudioError(tr("can't read the length of %s") % Path(src).name)


def has_audio(src) -> bool:
    _rc, err = _run([ffmpeg_exe(), "-hide_banner", "-i", str(src)])
    return "Audio:" in err


def _atempo(ratio: float) -> list:
    # 老版本 ffmpeg 的 atempo 只吃 0.5..2, 超了就串几个
    out = []
    while ratio > 2.0:
        out.append("atempo=2.0")
        ratio /= 2.0
    while ratio < 0.5:
        out.append("atempo=0.5")
        ratio /= 0.5
    out.append(f"atempo={ratio:.5f}")
    return out


def extract(src, out_wav, duration: float = None, loud=True) -> Path:
    """-> 44.1k 立体声 16bit WAV. duration = 特效长度, 给了就把声音变速对齐"""
    src, out_wav = Path(src), Path(out_wav)
    if src.is_dir():
        raise MDStudioError(tr("a frame folder has no sound. Untick 'use the video's own audio' "
                              "or pick a sound."))
    if not has_audio(src):
        raise MDStudioError(tr("%s has no audio track") % src.name)
    step(tr("taking the sound out of %s"), src.name)
    filters = []
    if duration:
        ratio = video_length(src) / float(duration)
        if abs(ratio - 1) > 0.01:
            filters += _atempo(ratio)
    if loud:
        filters += ["acompressor=threshold=-20dB:ratio=4:attack=5:release=120", "loudnorm=I=-8:TP=-1.0:LRA=11",
                    "alimiter=limit=0.97"]
    args = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn",
            "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le"]
    if filters:
        args += ["-af", ",".join(filters)]
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    rc, err = _run(args + [str(out_wav)])
    if rc != 0 or not out_wav.is_file():
        raise MDStudioError(tr("ffmpeg couldn't take the sound out: %s") % err.strip()[-300:])
    return out_wav
