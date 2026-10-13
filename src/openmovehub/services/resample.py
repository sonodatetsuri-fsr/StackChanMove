"""PCM16 mono のレート変換 (stackchan_move ``services/tts.py`` の線形補間をそのまま移したもの)。"""

import numpy as np


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """PCM16 little-endian mono を ``src_rate`` から ``dst_rate`` へ線形補間で変換する。"""
    if src_rate == dst_rate or not pcm:
        return pcm
    pcm_int16 = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
    original_length = len(pcm_int16)
    target_length = int(original_length * dst_rate / src_rate)
    if original_length == 0 or target_length == 0:
        return b""
    indices = np.linspace(0, original_length - 1, target_length)
    resampled = np.interp(indices, np.arange(original_length), pcm_int16.astype(np.float64))
    return resampled.astype(np.int16).tobytes()
