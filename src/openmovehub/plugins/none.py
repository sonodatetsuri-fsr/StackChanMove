"""``none`` プラグイン (STT / TTS を使わない)。

- STT: 音声を受けても何もしない (``SttRunner`` は状態 ``off`` になり、VAD も回さない)。
- TTS: ``speak`` を受けたら ``SpeechQueue`` が警告を出して捨てる (``speak_start`` / ``speak_end`` も送らない)。
"""

from typing import Any

from openmovehub.plugins.base import TtsAudio

NONE_NAME = "none"


class NoneStt:
    name = NONE_NAME

    def load(self) -> None:
        return None

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return ""

    def unload(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {}


class NoneTts:
    name = NONE_NAME

    async def synthesize(self, text: str) -> TtsAudio:
        raise RuntimeError("TTS plugin is none")

    async def probe(self) -> dict[str, Any]:
        return {"ok": False, "message": "disabled"}

    async def close(self) -> None:
        return None


def create_stt(settings: Any = None) -> NoneStt:
    return NoneStt()


def create_tts(settings: Any = None) -> NoneTts:
    return NoneTts()
