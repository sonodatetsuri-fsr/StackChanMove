"""テスト用の第三者プラグイン (``module:factory`` 指定と entry point のモック用)。"""

from typing import Any

from openmovehub.plugins.base import TtsAudio


class DummyStt:
    name = "dummy"

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return "dummy"

    def unload(self) -> None:
        self.loaded = False

    def status(self) -> dict[str, Any]:
        return {"kind": "dummy"}


class DummyTts:
    name = "dummy"

    async def synthesize(self, text: str) -> TtsAudio:
        return TtsAudio(pcm=b"\x00\x00" * 10, sample_rate=16000)

    async def probe(self) -> dict[str, Any]:
        return {"ok": True, "message": "dummy"}

    async def close(self) -> None:
        return None


def create_stt(settings: Any = None) -> DummyStt:
    return DummyStt(settings)


def create_tts(settings: Any = None) -> DummyTts:
    return DummyTts()


def broken_factory(settings: Any = None) -> Any:
    raise RuntimeError("factory exploded")
