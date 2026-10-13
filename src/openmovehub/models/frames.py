"""WebSocket バイナリフレームのヘッダ (kind + ts_ms)。

全方向のバイナリフレームは 5 バイトのヘッダを持つ::

    [0]    kind   uint8   0x01=PCM16/16kHz/mono, 0x02=JPEG
    [1..4] ts_ms  uint32 LE  送信側の単調時刻 (ms)
    [5..]  payload

これにより音声 (PCM) とカメラ画像 (JPEG) を単一の WebSocket に多重化できる。
"""

import struct
import time

HEADER_SIZE = 5
KIND_PCM = 0x01
KIND_JPEG = 0x02

_HEADER_STRUCT = struct.Struct("<BI")


def monotonic_ms() -> int:
    """送信側の単調時刻 (ms) を uint32 に収めて返す。"""
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def encode_frame(kind: int, payload: bytes, ts_ms: int | None = None) -> bytes:
    """``kind`` / ``ts_ms`` のヘッダを付けたバイナリフレームを組み立てる。"""
    if ts_ms is None:
        ts_ms = monotonic_ms()
    return _HEADER_STRUCT.pack(kind & 0xFF, ts_ms & 0xFFFFFFFF) + payload


def decode_frame(data: bytes) -> tuple[int, int, bytes]:
    """バイナリフレームを ``(kind, ts_ms, payload)`` に分解する。

    ヘッダに満たない (5 バイト未満の) フレームは ``ValueError``。
    """
    if len(data) < HEADER_SIZE:
        raise ValueError(f"Binary frame shorter than header: {len(data)} bytes")
    kind, ts_ms = _HEADER_STRUCT.unpack_from(data, 0)
    return kind, ts_ms, bytes(data[HEADER_SIZE:])
