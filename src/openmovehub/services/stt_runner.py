"""STT プラグインの Hub 共通処理。

- ``load()`` をバックグラウンドスレッドで 1 回だけ実行し、状態 ``loading | ready | error | off`` を持つ
  (``none`` プラグインは ``off``)。``load()`` の例外は ``error`` にして Hub は止めない。
- ``ready`` 前に届いた発話は破棄する (CPU 環境の初回ロード中の発話が後からまとめて認識されないように)。
- NG ワード (``stt_ng_words.txt``、完全一致) を除く (どのプラグインにも効く)。
- heard フック (``async def on_heard(text) -> str``、intent を返す) を 1 つ持つ。既定は無し (intent は空)。
- 状態が変わったら ``notify(state)`` (app へ ``state stt:<state>`` を送る) を呼ぶ。
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from openmovehub.plugins.none import NONE_NAME

logger = logging.getLogger(__name__)

SttState = Literal["loading", "ready", "error", "off"]
HeardHook = Callable[[str], Awaitable[str]]
StateNotify = Callable[[str], Awaitable[None]]


def load_ng_words(path: str) -> set[str]:
    ng_path = Path(path)
    if not ng_path.exists():
        logger.info("NG words file not found: %s (filter disabled)", path)
        return set()
    words: set[str] = set()
    for line in ng_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            words.add(stripped)
    logger.info("Loaded %d NG words from %s", len(words), path)
    return words


def _format_status(status: dict[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in status.items())


class SttRunner:
    def __init__(
        self,
        plugin: Any,
        label: str | None = None,
        ng_words: set[str] | None = None,
        notify: StateNotify | None = None,
    ) -> None:
        self.plugin = plugin
        self.label = label if label is not None else plugin.name
        self._ng_words = ng_words or set()
        self._notify = notify
        self._state: SttState = "off" if plugin.name == NONE_NAME else "loading"
        self.error: str | None = None
        self.heard_hook: HeardHook | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped_not_ready = 0

    # --- 状態 ---------------------------------------------------------------

    @property
    def state(self) -> SttState:
        return self._state

    @property
    def accepts_audio(self) -> bool:
        """VAD を回す価値があるか (``off`` 以外)。"""
        return self._state != "off"

    def _set_state(self, state: SttState) -> None:
        if state == self._state:
            return
        self._state = state
        if self._notify is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._notify(state), self._loop)
        except RuntimeError:  # pragma: no cover - ループ終了後
            logger.debug("STT state notify skipped (loop closed)")

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {"plugin": self.label, "state": self._state}
        try:
            status.update(self.plugin.status())
        except Exception:
            logger.exception("STT plugin status() failed")
        if self.error is not None:
            status["error"] = self.error
        return status

    # --- ロード -------------------------------------------------------------

    def start(self) -> None:
        """バックグラウンドでロードを始める (1 回だけ)。``off`` なら何もしない。"""
        if self._state == "off":
            logger.info("STT plugin: %s (off)", self.label)
            return
        if self._thread is not None:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        logger.info("STT plugin: %s (%s) loading", self.label, _format_status(self.plugin.status()))
        self._thread = threading.Thread(target=self._load, name="stt-load", daemon=True)
        self._thread.start()

    def _load(self) -> None:
        try:
            self.plugin.load()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            logger.exception("STT load failed (%s)", self.label)
            self._set_state("error")
            return
        logger.info("STT ready (%s)", _format_status(self.plugin.status()))
        self._set_state("ready")

    def wait_loaded(self, timeout: float | None = None) -> bool:
        """テスト・終了処理用: ロードスレッドの終了を待つ。"""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def close(self) -> None:
        """Hub の終了時にだけ呼ぶ (``unload()``)。"""
        if self._state == "ready":
            try:
                self.plugin.unload()
            except Exception:
                logger.exception("STT unload failed")

    # --- 認識 ---------------------------------------------------------------

    def is_ng_word(self, text: str) -> bool:
        return text.strip() in self._ng_words

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """発話 1 つを認識する。``ready`` 前の発話・NG ワードは ``""``。"""
        if self._state != "ready":
            self.dropped_not_ready += 1
            logger.info("STT not ready (state=%s); utterance dropped", self._state)
            return ""
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, self.plugin.transcribe, pcm, sample_rate)
        if self.is_ng_word(text):
            logger.debug("Filtered NG word: %s", text)
            return ""
        return text

    async def on_heard(self, text: str) -> str:
        """heard フックを呼んで intent を返す (フック無し・例外は ``""``)。"""
        hook = self.heard_hook
        if hook is None:
            return ""
        try:
            return await hook(text) or ""
        except Exception:
            logger.exception("heard hook failed")
            return ""
