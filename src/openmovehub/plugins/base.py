"""STT / TTS プラグインのインタフェース。

プラグインは「PCM → テキスト」「テキスト → PCM」だけを受け持つ。VAD・NG ワード・16 kHz への変換・
ロード状態の管理・端末への送信ペーシングは Hub 共通処理 (``services/``) が行う。

各プラグインはファクトリ関数 ``create(settings: Settings) -> SttPlugin | TtsPlugin`` を公開する。
プラグイン固有の設定はプラグイン自身が env から読む。
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class TtsAudio:
    """合成結果。``pcm`` は PCM16 little-endian / mono、``sample_rate`` は Hz。"""

    pcm: bytes
    sample_rate: int


@runtime_checkable
class SttPlugin(Protocol):
    name: str

    def load(self) -> None:
        """重い初期化 (モデルのロード)。Hub がバックグラウンドスレッドで 1 回だけ呼ぶ。"""
        ...

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """発話 1 つ (PCM16 mono) を認識する。同期。Hub が executor で呼ぶ。"""
        ...

    def unload(self) -> None:
        """モデルを解放する (Hub の終了時に呼ぶ)。"""
        ...

    def status(self) -> dict[str, Any]:
        """表示用の情報 (model, device など)。``/status.audio.stt`` に展開する。"""
        ...


@runtime_checkable
class TtsPlugin(Protocol):
    name: str

    async def synthesize(self, text: str) -> TtsAudio:
        """テキストを合成する。失敗は例外で返す (Hub は警告して飛ばす)。"""
        ...

    async def probe(self) -> dict[str, Any]:
        """エンジンの疎通確認。``{"ok": bool, "message": str, ...}``。起動時と 30 s ごとに呼ぶ。"""
        ...

    async def close(self) -> None: ...
