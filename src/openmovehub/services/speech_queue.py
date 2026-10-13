"""発話キュー (stackchan_move ``services/speech_queue.py`` を TTS プラグイン経由に書き換えたもの)。

``listen off`` → TTS → (16 kHz 以外なら変換) → ``speak_start`` → 実時間ペースで送信 → ``speak_end`` →
``listen`` を元に戻す。``speak_start`` は音声ができてから送る (合成の後に送る。合成の間に端末の
見張りが発話を止めたり、口パクが先に動いたりしないため)。``synthesize()`` や変換が例外を出した発話は警告して
飛ばし、``speak_start`` / ``speak_end`` は送らない。``listen`` の復帰は必ず送り、``speak_start`` を送った発話には
送信の途中で例外が起きても ``speak_end`` を送る。TTS が ``none`` のときは ``speak`` を警告して捨てる
(``speak_start`` / ``speak_end`` も送らない)。
"""

import asyncio
import logging
from typing import Any

from openmovehub.config import Settings
from openmovehub.models.frames import KIND_PCM, encode_frame
from openmovehub.models.messages import ListenMessage, SpeakEndMessage, SpeakStartMessage
from openmovehub.plugins.none import NONE_NAME
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.listen_state import ListenState
from openmovehub.services.resample import resample_pcm16

logger = logging.getLogger(__name__)


class SpeechQueue:
    def __init__(
        self,
        tts: Any,
        manager: ConnectionManager,
        settings: Settings,
        listen_state: ListenState,
    ) -> None:
        self._tts = tts
        self._manager = manager
        self._settings = settings
        self._listen_state = listen_state
        self._queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, text: str, emotion: str | None = None) -> None:
        if getattr(self._tts, "name", None) == NONE_NAME:
            logger.warning("speak dropped (TTS plugin is none): %s", text[:50])
            return
        await self._queue.put((text, emotion))

    async def _send_paced(self, audio: bytes) -> None:
        """Send PCM audio to devices split into real-time-paced slices.

        A leading prebuffer is sent up front (absorbs network/event-loop jitter) as back-to-back
        slice-sized frames, then the
        remainder is sent slice by slice with ``asyncio.sleep(slice_s)`` between slices so the
        device playback ring never overflows regardless of utterance length.
        """
        bytes_per_sec = self._settings.sample_rate * self._settings.channels * 2
        # Align slice boundaries to int16 samples (channels * 2 bytes per sample frame).
        frame_bytes = self._settings.channels * 2
        slice_bytes = int(self._settings.speak_slice_s * self._settings.sample_rate) * frame_bytes
        slice_bytes = max(slice_bytes, frame_bytes)
        # Round prebuffer up to a whole number of slices.
        prebuffer_bytes = int(self._settings.speak_prebuffer_s * bytes_per_sec)
        prebuffer_slices = max(1, -(-prebuffer_bytes // slice_bytes))  # ceil division
        prebuffer_bytes = prebuffer_slices * slice_bytes

        total = len(audio)
        head = audio[:prebuffer_bytes]
        # 先送り分もスライスと同じ大きさのフレームに分け、間を空けずに続けて送る
        # (大きな 1 フレームは端末の WebSocket クライアントの送受信を長く止めるため)。
        prebuffer_frames = 0
        for start in range(0, len(head), slice_bytes):
            await self._manager.send_to_devices(encode_frame(KIND_PCM, head[start : start + slice_bytes]))
            prebuffer_frames += 1
        offset = len(head)
        slice_count = 0
        while offset < total:
            chunk = audio[offset : offset + slice_bytes]
            await asyncio.sleep(self._settings.speak_slice_s)
            await self._manager.send_to_devices(encode_frame(KIND_PCM, chunk))
            offset += len(chunk)
            slice_count += 1
        logger.info(
            "Paced audio sent: %d bytes, prebuffer=%d bytes, %d paced slices, prebuffer_frames=%d",
            total,
            len(head),
            slice_count,
            prebuffer_frames,
        )

    async def _synthesize(self, text: str) -> bytes | None:
        """合成して 16 kHz の PCM を返す。失敗したら警告して None。"""
        try:
            audio = await self._tts.synthesize(text)
            pcm = audio.pcm
            if audio.sample_rate != self._settings.sample_rate:
                pcm = resample_pcm16(pcm, audio.sample_rate, self._settings.sample_rate)
        except Exception as e:
            logger.warning("TTS synthesize failed (%s: %s); skipped: %s", type(e).__name__, e, text[:50])
            return None
        return pcm

    async def _worker(self) -> None:
        while True:
            text, _emotion = await self._queue.get()
            try:
                await self._manager.send_json_to_devices(ListenMessage(type="listen", on=False))
                try:
                    pcm = await self._synthesize(text)
                    if pcm is not None:
                        await self._manager.send_json_to_devices(SpeakStartMessage(type="speak_start"))
                        try:
                            await self._send_paced(pcm)
                        finally:
                            await self._manager.send_json_to_devices(SpeakEndMessage(type="speak_end"))
                finally:
                    await self._manager.send_json_to_devices(ListenMessage(type="listen", on=self._listen_state.on))
            except Exception:
                logger.exception("Speech processing failed for text: %s", text[:50])
            finally:
                self._queue.task_done()
