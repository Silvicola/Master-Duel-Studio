"""音效: seLabel -> Sound/AudioClip/SE_DUEL/<label>, AudioClip 数据是 bundle 里 .resource 的 FSB5

自己造一个槽是可行的, 但必须同时在 data.unity3d 的 SE_DUEL_Resources 白名单里登记
(见 core/se_labels.py)。2026-11-13 实测: 文件放对、格式是官方 Vorbis、条目也指过去了,
只要白名单里没有名字, 游戏就不会去读它, 结果静音; 表里加一条立刻有声。
PCM16 在白名单下同样能播, 所以自定义音频不需要做 Vorbis 编码。
"""
import logging
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import UnityPy
from UnityPy.streams import EndianBinaryReader

from mdstudio.core.crc import get_crc
from mdstudio.errors import AssetMissing, MDStudioError, NotOurs
from mdstudio.i18n import tr
from mdstudio.ops.tx import Tx, undo

log = logging.getLogger("mdstudio")

SE_DIR = "Sound/AudioClip/SE_DUEL"
FREQ = {8000: 1, 11000: 2, 11025: 3, 16000: 4, 22050: 5, 24000: 6, 32000: 7, 44100: 8, 48000: 9}


def clip_path(label: str) -> str:
    return f"{SE_DIR}/{label}"


# 这 4 个最老的特效音不在 LocalData, 打在 masterduel_Data/data.unity3d 里 (UnityPy 读出来确认过),
# asset 这条路换不了. 列出来只是为了报错时说清楚, 别让人以为是没下载
BUILTIN = {"SE_EV_MONSTER_REBORN", "SE_EV_BLACKHOLE", "SE_EV_GOFUKEN", "SE_EV_THOUSANDKNIVES"}


def on_disk(game, label: str) -> bool:
    return bool(game.find_logical(clip_path(label)))


def why_missing(label: str) -> str:
    if label in BUILTIN:
        return tr("%s is built into the game app (data.unity3d), not a downloaded file, so it can't "
                  "be replaced. Give the effect another sound label and replace that one instead.") % label
    return tr("%s: clip not on disk (not downloaded, or a new label). It would be silent. "
              "Pick one that is.") % label


def clip_wav(game, label: str) -> bytes:
    """独立文件那个槽的音频，返回整段 wav。内置的那 4 条不在其中（解不开）"""
    src = game.find_logical(clip_path(label))
    if src is None:
        raise AssetMissing(why_missing(label))
    env = UnityPy.load(src.read_bytes())
    for o in env.objects:
        if o.type.name != "AudioClip":
            continue
        for _, raw in o.read().samples.items():
            return raw
        break
    raise MDStudioError(tr("%s has no decodable audio") % label)


def preview_clip(ctx, label: str) -> str:
    """把某个槽的音频解成临时 wav，供界面试听。

    只读游戏文件，不写任何东西到游戏目录。内置在 data.unity3d 里的那 4 条也能解，
    代价是首次要解析一次 128MB 的安装包（实测 0.7 秒），之后同进程内走缓存。
    """
    from uuid import uuid4
    from mdstudio import runtime

    label = (label or "").strip()
    if not label:
        raise MDStudioError(tr("no sound label given"))
    if label in BUILTIN:
        # 这 4 条打包在 data.unity3d 的 resources.resource 里，FSB5 用 fmod 解不开，
        # 我们也没有自带的 Vorbis 解码器，所以只能借用、不能试听
        raise MDStudioError(tr("%s is built into the game app and cannot be previewed") % label)
    blob = clip_wav(ctx.game, label)

    d = runtime.data_dir() / "sound" / "preview"
    d.mkdir(parents=True, exist_ok=True)
    # 每次换个文件名：播放器会占着旧文件，同名覆盖会失败
    for old in d.glob(f"{label}_*.wav"):
        try:
            old.unlink()
        except OSError:
            pass
    out = d / f"{label}_{uuid4().hex[:8]}.wav"
    out.write_bytes(blob)
    return str(out)



def build_fsb5_pcm16(pcm: bytes, channels: int, rate: int) -> bytes:
    """最小 FSB5, mode 2 = PCM16. 用 FMOD 解过, 逐字节对得上"""
    if rate not in FREQ or channels not in (1, 2):
        raise ValueError(tr("rate/channels not supported by FSB5"))
    frames = len(pcm) // (2 * channels)
    # sample header: frames<<34 | offset<<7 | (ch-1)<<5 | freq<<1 | hasExtra
    sample_hdr = struct.pack("<Q", (frames << 34) | ((channels - 1) << 5) | (FREQ[rate] << 1))
    data = pcm + b"\x00" * ((-len(pcm)) % 32)
    header = struct.pack("<4sIIIIII", b"FSB5", 1, 1, len(sample_hdr), 0, len(data), 2)
    header += struct.pack("<Q", 1) + b"\x00" * 24   # flags + hash(16) + dummy(8) -> 头 60 字节
    return header + sample_hdr + data


def read_wav(path):
    """-> (pcm16 bytes, channels, rate). 只吃整数 PCM wav, float 的先用 ffmpeg 转"""
    try:
        with wave.open(str(path), "rb") as w:
            ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise MDStudioError(tr("%s: needs a plain PCM .wav (%s). Convert it: "
                              "ffmpeg -i in.mp3 -c:a pcm_s16le out.wav") % (path, e)) from None
    if ch > 2:
        raise MDStudioError(tr("WAV must be mono or stereo"))
    if sw == 1:
        a = (np.frombuffer(raw, np.uint8).astype(np.int32) - 128) << 8
    elif sw == 2:
        a = np.frombuffer(raw, "<i2").astype(np.int32)
    elif sw == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        a = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        a = np.where(a & 0x800000, a - 0x1000000, a) >> 8
    elif sw == 4:
        a = np.frombuffer(raw, "<i4").astype(np.int64) >> 16
    else:
        raise MDStudioError(tr("unsupported WAV sample width %s") % sw)
    a = a.reshape(-1, ch).astype(np.float64)
    if rate not in FREQ:
        # 线性重采样到 48k, 音效够用了
        n_out = int(round(len(a) * 48000 / rate))
        t = np.linspace(0, len(a) - 1, n_out)
        a = np.stack([np.interp(t, np.arange(len(a)), a[:, c]) for c in range(ch)], axis=1)
        rate = 48000
    pcm = np.clip(np.round(a), -32768, 32767).astype("<i2").tobytes()
    return pcm, ch, rate


def inject(ctx, label: str, wav_path, card: int = 0):
    """card = 这次换音是为了哪张卡 (视频自带声音), 记上它, rm 那张卡时一起还原"""
    tx = Tx(ctx, "sound.inject", card)
    src = tx.game.find_logical(clip_path(label))
    if not src:
        raise AssetMissing(why_missing(label))
    if any(r.meta.get("label") == label for r in ctx.journal.load() if r.op == "sound.inject"):
        raise MDStudioError(tr("%s was already replaced by this tool. Restore it first.") % label)
    pcm, ch, rate = read_wav(wav_path)
    fsb = build_fsb5_pcm16(pcm, ch, rate)
    length = len(pcm) / (2 * ch) / rate

    env = UnityPy.load(src.read_bytes())
    bf = env.file
    res = [n for n in bf.files if n.lower().endswith(".resource")]
    if not res:
        raise MDStudioError(tr("%s: no .resource in clip bundle") % label)
    old = bf.files[res[0]]
    new = EndianBinaryReader(fsb)
    new.flags = old.flags
    bf.files[res[0]] = new
    for o in env.objects:
        if o.type.name == "AudioClip":
            tt = o.read_typetree()
            tt.update({"m_Channels": ch, "m_Frequency": rate, "m_BitsPerSample": 16, "m_Length": length,
                       "m_CompressionFormat": 0, "m_LoadType": 0, "m_PreloadAudioData": True})
            tt["m_Resource"]["m_Size"] = len(fsb)
            tt["m_Resource"]["m_Offset"] = 0
            o.save_typetree(tt)
    tx.put_existing(src, bf.save(packer="lz4"))
    tx.rec.meta = {"label": label, "seconds": round(length, 2)}
    return tx.commit()


def restore(ctx, label: str):
    recs = [r for r in ctx.journal.load() if r.op == "sound.inject" and r.meta.get("label") == label]
    if not recs:
        raise NotOurs(tr("%s: nothing to restore") % label)
    undo(ctx, recs)


def labels_of_card(gate, card: int) -> list:
    return sorted({e.get("seLabel") for e in gate.for_card(card) if e.get("seLabel")})


def loudness_hint() -> str:
    # 不压一下在游戏里很小声
    return ('ffmpeg -i in.wav -af "acompressor=threshold=-20dB:ratio=4:attack=5:release=120,'
            'loudnorm=I=-8:TP=-1.0:LRA=11,alimiter=limit=0.97" -acodec pcm_s16le -ar 48000 -ac 2 out.wav')


# ---------------------------------------------------------------- 自定义槽

CUSTOM_PREFIX = "SE_EV_CUSTOM"
CLIP_RATE = 48000
CLIP_MAX_SECONDS = 6.0     # 官方音效最长 5.7 秒, 超过这个长度多半会被特效结束切断


def custom_label(card: int) -> str:
    """自定义槽的名字按卡固定。重复导入就覆盖内容, 不在白名单里越堆越长"""
    return f"{CUSTOM_PREFIX}{int(card)}"


def _ffmpeg_pcm(path) -> tuple:
    """任意音频转成 2ch/48k/16bit, 返回 (pcm16, 声道, 采样率, 秒数)"""
    from mdstudio.ops.video import audio as A
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "clip.wav"
        r = subprocess.run([A.ffmpeg_exe(), "-y", "-i", str(path),
                            "-ac", "2", "-ar", str(CLIP_RATE), "-acodec", "pcm_s16le",
                            str(out)], capture_output=True, text=True)
        if r.returncode != 0 or not out.is_file():
            raise MDStudioError(tr("%s is not a readable audio file") % Path(path).name)
        with wave.open(str(out), "rb") as w:
            ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
            pcm = w.readframes(n)
    return pcm, ch, rate, n / rate


def read_audio(path) -> tuple:
    """已经是 16bit PCM wav 就直接读, 省一次转码"""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() == 2:
                ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
                return w.readframes(n), ch, rate, n / rate
    except (wave.Error, OSError, EOFError):
        pass
    return _ffmpeg_pcm(path)


def _template_clip(ctx):
    """拿一个盘上有的 SE_EV bundle 当容器。它只提供包结构和字段形状, 内容整体替换"""
    from mdstudio.core.gate import RunFxGate
    gate = RunFxGate(ctx.game)
    seen = set()
    for e in gate.entries:
        lb = e.get("seLabel") or ""
        if not lb.startswith("SE_EV") or lb in BUILTIN or lb in seen:
            continue
        seen.add(lb)
        p = ctx.game.find_logical(clip_path(lb))
        if p:
            return p
    raise AssetMissing(tr("no sound bundle on disk to use as a template"))


def build_clip(label: str, audio_path, template: Path) -> tuple:
    """造一个只含这段音频的 bundle, 返回 (字节, 秒数)"""
    pcm, ch, rate, seconds = read_audio(audio_path)
    if seconds <= 0:
        raise MDStudioError(tr("%s has no audio") % Path(audio_path).name)
    fsb = build_fsb5_pcm16(pcm, ch, rate)
    env = UnityPy.load(Path(template).read_bytes())
    bf = env.file
    res = [n for n in bf.files if n.lower().endswith(".resource")]
    if not res:
        raise MDStudioError(tr("the template bundle has no .resource"))
    nr = EndianBinaryReader(fsb)
    nr.flags = bf.files[res[0]].flags
    bf.files[res[0]] = nr
    for o in env.objects:
        if o.type.name != "AudioClip":
            continue
        tt = o.read_typetree()
        tt.update({"m_Name": label, "m_Channels": ch, "m_Frequency": rate,
                   "m_BitsPerSample": 16, "m_Length": seconds,
                   "m_CompressionFormat": 0, "m_LoadType": 0, "m_PreloadAudioData": True})
        tt["m_Resource"]["m_Size"] = len(fsb)
        tt["m_Resource"]["m_Offset"] = 0
        o.save_typetree(tt)
    return env.file.save(packer="lz4"), seconds


def sync_se_label(ctx, card: int, entries) -> None:
    """把刚改过的 gate 条目的 seLabel 抄回 journal 里的 runfx 记录。

    坑：`seLabel` 在 `runfx_sig` 里（core/gate.py）。只改游戏表、不改记录，
    之后点「复原」时按签名一条都匹配不上：gate 清不掉，而 bundle 照样被删，
    游戏一进对局就报「文件读取错误 EfNNNNN」，卡片还会因为「有登记无记录」
    显示成官方特效、复原按钮变灰。

    配对方式是不含 seLabel 的那几个字段，所以逐条不同音效（还原原音）也对得上。
    """
    if ctx.dry_run or not entries:
        return
    from mdstudio.core.gate import RUNFX_SIG
    from mdstudio.ops import runfx as R

    keys = tuple(k for k in RUNFX_SIG if k != "seLabel")
    want = {tuple(e.get(k) for k in keys): (e.get("seLabel") or "") for e in entries}
    recs = [r for r in ctx.journal.for_card(card, R.OPS) if r.runfx_added]
    changed = False
    for r in recs:
        for e in r.runfx_added:
            k = tuple(e.get(k) for k in keys)
            if k in want and (e.get("seLabel") or "") != want[k]:
                e["seLabel"] = want[k]
                changed = True
    if changed:
        ctx.journal.update(recs)


def create_slot(ctx, card: int, audio_path, label: str = "", volume: float = 1.0,
                source_name: str = ""):
    """给这张卡建专属音效槽并让它用上。

    前提是这张卡已经有发动特效的条目, 音效挂在条目上; 没有就先去做特效。
    source_name 只用于界面显示（「自改音效：千把刀」），不给就取文件名。
    """
    from mdstudio.core import se_labels as SL
    label = (label or custom_label(card)).strip()
    if not label:
        raise MDStudioError(tr("the sound name cannot be empty"))
    blob, seconds = build_clip(label, audio_path, _template_clip(ctx))
    # 改指向之前先把原值记下来, 否则"本卡原音"会被记成刚建的自定义槽
    remember_origin(ctx, card)

    tx = Tx(ctx, "sound.custom", card)
    # 只换「自己」那条。对手那条（player=1）要留着原样，不能跟着改
    entries = self_entries(tx.runfx_gate.for_card(card))
    if not entries:
        raise MDStudioError(tr("%s has no activation effect to attach a sound to") % card)
    for e in entries:
        e["seLabel"] = label
    sync_se_label(ctx, card, entries)
    tx.put_file(get_crc(clip_path(label)), blob)
    tx.rec.meta = {"label": label, "seconds": round(seconds, 2), "rate": CLIP_RATE,
                   # 记下来源文件名, 界面显示「自改音效：菲比啾比」要用
                   "source": source_name or Path(audio_path).stem}
    if not ctx.dry_run:
        # 白名单先占位: 万一后面写文件失败, 多一条没人引用的登记无害;
        # 反过来先写文件再登记, 失败时条目指向一个读不到的槽, 表现是静音, 更难排查
        SL.add(ctx.game, label, volume)
    return tx.commit()


def adopt_label(ctx, card: int, label: str):
    """把别的槽的音频复制成这张卡自己的槽，原槽不受影响。

    这样「用别人那张卡的音效」不会变成两张卡共用一个文件，
    和去掉「借用特效」是同一条理由：不共享，各改各的。
    """
    wav = clip_wav(ctx.game, label)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"{label}.wav"
        p.write_bytes(wav)
        return create_slot(ctx, card, p, source_name=label)


def drop_slot(ctx, label: str):
    """删掉自己建的槽: 白名单去掉登记, 文件交给撤销流程"""
    from mdstudio.core import se_labels as SL
    if not label.startswith(CUSTOM_PREFIX):
        raise NotOurs(tr("%s is not a custom sound slot") % label)
    if ctx.dry_run:
        return False
    return SL.remove(ctx.game, label)


# ---------------------------------------------------------------- 预设素材库
# 预设就是一段音频文件, 和内置的三个 mp3 完全平等: 应用时同样走 create_slot 转码。
# 内置那份随包放在 tools/sounds/presets (打包时整目录带走, 只读);
# 用户存的放 data/sound/presets, 跟着程序目录搬家, 换版本时构建脚本会继承 data。

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg")


def builtin_presets_dir():
    from mdstudio import runtime
    return Path(runtime.bundle_root()) / "tools" / "sounds" / "presets"


def user_presets_dir():
    from mdstudio import runtime
    d = runtime.data_dir() / "sound" / "presets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _presets_index_file():
    return user_presets_dir().parent / "presets.json"


def _load_preset_index() -> dict:
    import json
    try:
        return json.loads(_presets_index_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_preset_index(data: dict):
    import json
    try:
        _presets_index_file().write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def audio_seconds(path) -> float:
    """读音频时长。只为界面显示, 读不出来就给 0, 不让它挡住保存"""
    try:
        from mdstudio.ops.video.audio import video_length
        return round(video_length(path), 2)
    except Exception:
        return 0.0


def list_presets() -> list:
    """内置 + 用户存的, 合并成一份。用户存的按存入时间倒序排在最前。

    时长只探测一次然后写进索引: 每次跑一次 ffmpeg 要 100ms 上下, 预设多了
    开弹窗就会卡。按「路径 + 大小」记缓存, 内置文件被换过就重新探。
    """
    index = _load_preset_index()
    cache = index.get("_seconds") or {}
    dirty = False

    def seconds_of(p):
        nonlocal dirty
        key = str(p)
        size = p.stat().st_size
        hit = cache.get(key)
        if isinstance(hit, list) and len(hit) == 2 and hit[1] == size:
            return hit[0]
        val = audio_seconds(p)
        cache[key] = [val, size]
        dirty = True
        return val

    out = []
    bdir = builtin_presets_dir()
    if bdir.is_dir():
        for p in sorted(bdir.iterdir()):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                out.append({"name": p.stem, "path": str(p), "builtin": True,
                            "seconds": seconds_of(p)})
    mine = []
    for p in user_presets_dir().iterdir():
        if not (p.is_file() and p.suffix.lower() in AUDIO_EXTS):
            continue
        meta = index.get(p.name) or {}
        mine.append({"name": meta.get("name") or p.stem, "path": str(p), "builtin": False,
                     "seconds": meta.get("seconds") or seconds_of(p),
                     "saved": meta.get("saved") or 0})
    if dirty:
        index["_seconds"] = cache
        _save_preset_index(index)
    mine.sort(key=lambda r: r["saved"], reverse=True)
    return mine + out


VIDEO_EXTS = (".mp4", ".gif", ".webm", ".mov", ".mkv")


def extract_video_track(path, duration=None) -> str:
    """把视频/动图的音轨抽成 wav，供界面当成「当前导入」的候选。

    只是素材，不碰游戏文件。没有音轨、帧文件夹、路径不存在都返回空串，
    由界面静默跳过（用户没选音轨时不需要提示）。
    """
    from uuid import uuid4
    from mdstudio import runtime
    from mdstudio.ops.video import audio as A

    src = Path(path)
    if not src.is_file() or src.suffix.lower() not in VIDEO_EXTS:
        return ""
    try:
        if not A.has_audio(src):
            return ""
        d = runtime.data_dir() / "sound" / "preview"
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"track_{uuid4().hex[:8]}.wav"
        A.extract(src, out, duration=duration)
        return str(out)
    except Exception:
        # 抽不出来就当没音轨，别让选素材这一步失败
        return ""


def save_preset(name: str, audio_path) -> dict:
    """把一段音频存进素材库。只复制原件, 不转码: 应用时和内置预设走同一条路。

    名字撞上已有预设（含内置那三个）直接拒绝, 由界面提示改名,
    不做自动加后缀: 名字是用户自己起的, 替人改等于替他做决定。
    """
    import shutil
    import time
    src = Path(audio_path)
    if not src.is_file():
        raise MDStudioError(tr("%s does not exist") % src.name)
    name = (name or "").strip()
    if not name:
        raise MDStudioError(tr("the preset name cannot be empty"))
    for bad in '<>:"/\\|?*':
        if bad in name:
            raise MDStudioError(tr("a preset name cannot contain %s") % bad)
    taken = {r["name"].lower() for r in list_presets()}
    if name.lower() in taken:
        raise MDStudioError(tr("%s is already used by another preset") % name)

    ext = src.suffix.lower() if src.suffix.lower() in AUDIO_EXTS else ".mp3"
    dest = user_presets_dir() / f"{name}{ext}"
    shutil.copy2(src, dest)

    index = _load_preset_index()
    index[dest.name] = {"name": name, "seconds": audio_seconds(dest), "saved": int(time.time())}
    _save_preset_index(index)
    return {"name": name, "path": str(dest), "seconds": index[dest.name]["seconds"]}


def delete_preset(name: str) -> bool:
    """删掉用户存的那一条。内置的不给删, 也不碰游戏文件"""
    for r in list_presets():
        if r["builtin"] or r["name"] != name:
            continue
        p = Path(r["path"])
        try:
            p.unlink()
        except OSError:
            pass
        index = _load_preset_index()
        index.pop(p.name, None)
        _save_preset_index(index)
        return True
    raise NotOurs(tr("%s is not one of your presets") % name)


# ---------------------------------------------------------------- 换指向与复原

def _origins_file():
    from mdstudio import runtime
    d = runtime.data_dir() / "sound"
    d.mkdir(parents=True, exist_ok=True)
    return d / "origins.json"


def _load_origins() -> dict:
    import json
    p = _origins_file()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_origins(data: dict):
    import json
    p = _origins_file()
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def entry_key(e: dict) -> tuple:
    """一个条目的身份: player + 两侧资源路径 + 时机。

    不能只用路径: 同一张卡「自己」和「对手」两条可能指向同一个 Ef（#05166 两条
    都是 Ef05166）; 也不能只用 player: 时机不同的条目会撞。三个合起来才唯一。
    """
    from mdstudio.core.crc import resolve_illust

    return (e.get("player"),
            resolve_illust(e.get("effectPath") or ""),
            resolve_illust(e.get("centerEffectPath") or ""),
            e.get("runTiming"))


def self_entries(entries) -> list:
    """只取「自己发动」的那条。player=1 是对手（资源名带 Op 那批, 见 #4887/#14627）,
    2 是单条目卡、双方共用。改音效、换特效都只该动自己的, 对手那条原样留着。"""
    return [e for e in entries if e.get("player") != 1]


def origins_map(card: int, rows=None) -> dict:
    """这张卡的「原音」: {条目身份: 音效名}。

    老数据是纯字符串列表（按位置对），读到就按当前条目顺序换算，不写回。
    """
    raw = _load_origins().get(str(int(card)))
    if not raw:
        return {}
    if isinstance(raw[0], str):
        if not rows:
            return {}
        return {entry_key(e): lb for e, lb in zip(rows, raw)}
    out = {}
    for d in raw:
        if isinstance(d, dict):
            out[(d.get("p"), d.get("ep") or "", d.get("cp") or "", d.get("t"))] = d.get("se") or ""
    return out


def forget_origin(ctx, card: int):
    """扔掉这张卡的原音记录（卡片已经回到动手之前，旧映射没用了）"""
    if ctx.dry_run:
        return
    data = _load_origins()
    if data.pop(str(int(card)), None) is not None:
        _save_origins(data)


def remember_origin(ctx, card: int):
    """第一次改这张卡的音效前，把它各条目的 seLabel 记下来。

    撤销不能靠 gate 的整体备份（那份是所有卡共用的），只能记原值再写回。
    按「条目身份」记而不是按位置：之后重新生成也不会对错条目，对手那条不受影响。
    """
    from mdstudio.core.gate import RunFxGate

    rows = RunFxGate(ctx.game).for_card(card)
    data = _load_origins()
    key = str(int(card))
    if key in data:
        return origins_map(card, rows)
    data[key] = [{"p": entry_key(e)[0], "ep": entry_key(e)[1], "cp": entry_key(e)[2],
                  "t": entry_key(e)[3], "se": e.get("seLabel") or ""} for e in rows]
    _save_origins(data)
    return origins_map(card, rows)


def set_label(ctx, card: int, label: str):
    """改这张卡各条目的音效指向。label 传空串 = 无音效

    内置音效（BUILTIN 那几个）没有独立文件, 但游戏自己就能播, 不能按"盘上没有"拒掉。
    """
    label = (label or "").strip()
    if label and label not in BUILTIN and not on_disk(ctx.game, label):
        raise AssetMissing(why_missing(label))
    remember_origin(ctx, card)
    tx = Tx(ctx, "sound.set", card)
    # 同上：只改自己那条
    rows = self_entries(tx.runfx_gate.for_card(card))
    if not rows:
        raise MDStudioError(tr("%s has no activation effect to attach a sound to") % card)
    for e in rows:
        e["seLabel"] = label
    sync_se_label(ctx, card, rows)
    tx.rec.meta = {"label": label}
    return tx.commit()


def orphan_slot_cleanup(ctx, card: int, labels) -> bool:
    """复原音效后，把没人再引用的自建槽清掉：白名单、clip 文件、对应的 journal 记录。

    不清的话，游戏启动照样加载这条槽，盘上还多一段没人用的音频；改一次复原一次，
    垃圾就一直攒。只清「这张卡自己的槽」（SE_EV_CUSTOM<卡号>），别的卡、别人
    手写的名字一概不碰；只要还有任何一个条目指向它，就原样留着。
    """
    mine = custom_label(card)
    if mine not in {x or "" for x in labels}:
        return False
    from mdstudio.core.gate import RunFxGate

    if mine in {(e.get("seLabel") or "") for e in RunFxGate(ctx.game).entries}:
        return False
    if ctx.dry_run:
        return False
    drop_slot(ctx, mine)
    real = ctx.game.find_logical(clip_path(mine))
    if real:
        ctx.delete(real)
    ids = [r.id for r in ctx.journal.load()
           if r.card == card and r.op == "sound.custom"
           and (r.meta or {}).get("label") == mine]
    if ids:
        ctx.journal.drop(ids)
    log.info(tr("removed the unused custom sound slot %s"), mine)
    return True


def restore_label(ctx, card: int):
    """把这张卡的音效还原成记下来的原值。

    按条目身份配对，不看位置也不要求条数一致：重新生成过特效的卡，
    新条目自然配不上，跳过即可，不该弹「列表变了」那种看不懂的错。
    """
    tx = Tx(ctx, "sound.restore_card", card)
    rows = tx.runfx_gate.for_card(card)
    want = origins_map(card, rows)
    if not want:
        raise NotOurs(tr("%s: nothing recorded to restore") % card)
    # 复原前的指向，用来判断自建槽复原后是不是没人用了
    before = [e.get("seLabel") or "" for e in rows]
    hit = 0
    for e in rows:
        k = entry_key(e)
        if k in want:
            e["seLabel"] = want[k]
            hit += 1
    if not hit:
        # 条目已经被换掉了，记下来的原音对不上：清掉过期记录，界面会把按钮灰掉
        forget_origin(ctx, card)
        raise MDStudioError(tr("%s: the effect was replaced, so its original sound no longer "
                               "applies. Pick a sound again.") % card)
    sync_se_label(ctx, card, rows)
    tx.rec.meta = {"restored": [e.get("seLabel") or "" for e in rows]}
    rec = tx.commit()
    # 还原完，这条自建槽可能已经没有任何条目在用了；不清就是一份死文件加一条
    # 白名单登记，游戏启动还会把它加载进去。
    orphan_slot_cleanup(ctx, card, before)
    return rec


def apply_origins(ctx, card: int) -> int:
    """把记下来的原音写回现在的条目，然后扔掉这份记录。

    复原特效时用：undo 补回来的条目带的是「生成那一刻」的音效。如果你在那之前
    把官方音效从 Y 改成了 X，补回来的是 X，而你要的是 Y。这里按条目身份回滚一次。
    """
    from mdstudio.core.gate import RunFxGate

    if ctx.dry_run:
        forget_origin(ctx, card)
        return 0
    rows = RunFxGate(ctx.game).for_card(card)
    want = origins_map(card, rows)
    if not want:
        forget_origin(ctx, card)
        return 0
    tx = Tx(ctx, "sound.restore_card", card)
    hit = 0
    live = tx.runfx_gate.for_card(card)
    for e in live:
        k = entry_key(e)
        if k in want and (e.get("seLabel") or "") != want[k]:
            e["seLabel"] = want[k]
            hit += 1
    forget_origin(ctx, card)
    if not hit:
        return 0
    sync_se_label(ctx, card, live)
    tx.rec.meta = {"restored": [e.get("seLabel") or "" for e in live],
                   "why": "effect restored to the game's own"}
    tx.commit()
    return hit


def purge_preview_files() -> int:
    """清掉试听与抽轨留下的临时 wav，返回删掉几个。

    这两处都在 data/sound/preview 下写新名字的文件：
      - preview_clip：播放器会占着正在播的那份，同名覆盖会失败，所以每次换随机名，
        只顺手删同标签的旧文件；
      - extract_video_track：track_<rand>.wav，从来不删。
    不整体清理的话就会一直攒（一个音效几十到几百 KB）。
    进程刚起来时没有任何播放器持有句柄，是唯一能安全全删的时刻。
    """
    from mdstudio import runtime

    d = runtime.data_dir() / "sound" / "preview"
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob("*.wav"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass          # 还有句柄没释放就跳过，下次启动再清
    if n:
        log.info(tr("cleaned %s leftover preview sound file(s)"), n)
    return n


def drop_preview_files(card: int) -> int:
    """复原一张卡时，把它自己的音效试听文件一起删掉。

    只认 `SE_EV_CUSTOM<卡号>_*.wav`：自己挂的音效（含「用别人那张卡的音效」复制过来的）
    都落在以卡号命名的槽上，一张卡一个名字，删掉不会牵连别人。
    直接指向官方标签的那种不删，官方标签的临时文件可能别的卡也在用。

    复原的那一刻预览可能还开着、文件被播放器占着，删不掉就算了，
    引擎启动时那次整体清理会兜底。
    """
    from mdstudio import runtime

    d = runtime.data_dir() / "sound" / "preview"
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob(f"{custom_label(card)}_*.wav"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
