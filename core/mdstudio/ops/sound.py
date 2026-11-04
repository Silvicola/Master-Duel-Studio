"""音效: seLabel -> Sound/AudioClip/SE_DUEL/<label>, AudioClip 数据是 bundle 里 .resource 的 FSB5

坑: 全新的 label 不响. 游戏只认已经下载到本地的 clip, 所以只能借一个现成 label 换它的 clip.
挑个几乎没人用的, 不然原卡的声音也跟着变.
"""
import struct
import wave

import numpy as np
import UnityPy
from UnityPy.streams import EndianBinaryReader

from mdstudio.core.crc import get_crc
from mdstudio.errors import AssetMissing, MDStudioError, NotOurs
from mdstudio.i18n import tr
from mdstudio.ops.tx import Tx, undo

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
