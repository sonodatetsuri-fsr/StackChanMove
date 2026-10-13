"""``voicevox`` TTS プラグイン (VOICEVOX ENGINE)。

| env | 既定 | 説明 |
|---|---|---|
| ``VOICEVOX_URL`` | ``http://127.0.0.1:50021`` | |
| ``VOICEVOX_SPEAKER`` | ``1`` | 話者 (スタイル) ID |
| ``VOICEVOX_SPEED`` | ``1.0`` | ``audio_query`` の ``speedScale`` を上書き |
| ``TTS_TIMEOUT_S`` | ``120`` | |

``audio_query`` → ``synthesis`` は stackchan_move と同じ。返ってきた WAV は ``wave`` モジュールで読み、
サンプルレートを WAV ヘッダから取る (16 kHz への変換は Hub 共通処理)。
"""

import io
import json
import os
import wave
from typing import Any

import httpx
import numpy as np

from openmovehub.plugins.base import TtsAudio


def parse_wav(data: bytes) -> TtsAudio:
    """WAV (PCM16) を読んで mono の :class:`TtsAudio` にする。不正なら ``ValueError``。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"invalid WAV from VOICEVOX: {e}") from e
    if sampwidth != 2:
        raise ValueError(f"unsupported WAV sample width: {sampwidth * 8} bit (PCM16 expected)")
    if rate <= 0:
        raise ValueError(f"invalid WAV sample rate: {rate}")
    if channels > 1:
        samples = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
        frames = samples.mean(axis=1).astype(np.int16).tobytes()
    return TtsAudio(pcm=frames, sample_rate=rate)


def _find_speaker(speakers: Any, style_id: int) -> tuple[str | None, str | None]:
    """``GET /speakers`` の結果から話者 (キャラクター) 名とスタイル名を引く。"""
    if not isinstance(speakers, list):
        return None, None
    for speaker in speakers:
        for style in speaker.get("styles", []) if isinstance(speaker, dict) else []:
            if isinstance(style, dict) and style.get("id") == style_id:
                return speaker.get("name"), style.get("name")
    return None, None


class VoicevoxTts:
    name = "voicevox"

    def __init__(
        self,
        url: str = "http://127.0.0.1:50021",
        speaker: int = 1,
        speed: float = 1.0,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.speaker = speaker
        self.speed = speed
        self._client = httpx.AsyncClient(base_url=url, timeout=timeout_s, transport=transport)

    async def synthesize(self, text: str) -> TtsAudio:
        query_resp = await self._client.post("/audio_query", params={"text": text, "speaker": self.speaker})
        query_resp.raise_for_status()
        query = query_resp.json()
        query["speedScale"] = self.speed

        synthesis_resp = await self._client.post(
            "/synthesis",
            params={"speaker": self.speaker},
            content=json.dumps(query).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        synthesis_resp.raise_for_status()
        return parse_wav(synthesis_resp.content)

    async def probe(self) -> dict[str, Any]:
        base: dict[str, Any] = {"url": self.url, "speaker": self.speaker}
        try:
            resp = await self._client.get("/version", timeout=5.0)
            resp.raise_for_status()
            version = str(resp.json())
        except Exception as e:
            return {**base, "ok": False, "message": f"{type(e).__name__}: {e}"}
        speaker_name: str | None = None
        style_name: str | None = None
        try:
            speakers_resp = await self._client.get("/speakers", timeout=5.0)
            speakers_resp.raise_for_status()
            speaker_name, style_name = _find_speaker(speakers_resp.json(), self.speaker)
        except Exception:
            pass
        return {
            **base,
            "ok": True,
            "message": f"version {version}",
            "version": version,
            "speaker_name": speaker_name,
            "style_name": style_name,
        }

    async def close(self) -> None:
        await self._client.aclose()


def create(settings: Any = None) -> VoicevoxTts:
    return VoicevoxTts(
        url=os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021"),
        speaker=int(os.getenv("VOICEVOX_SPEAKER", "1")),
        speed=float(os.getenv("VOICEVOX_SPEED", "1.0")),
        timeout_s=float(os.getenv("TTS_TIMEOUT_S", "120")),
    )
