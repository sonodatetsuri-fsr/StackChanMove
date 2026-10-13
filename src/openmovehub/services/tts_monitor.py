"""TTS プラグインの疎通確認 (``probe()``) を起動時と一定間隔 (既定 30 s) で行う。

VOICEVOX ENGINE を Hub より後に起動しても ``/status.audio.tts`` に反映される。
ログは状態 (ok / NG) が変わったときだけ出す。
"""

import asyncio
import logging
from typing import Any

from openmovehub.plugins.none import NONE_NAME

logger = logging.getLogger(__name__)


class TtsMonitor:
    def __init__(self, plugin: Any, label: str | None = None, interval_s: float = 30.0) -> None:
        self.plugin = plugin
        self.label = label if label is not None else plugin.name
        self.interval_s = interval_s
        self._last: dict[str, Any] = {"ok": False, "message": "not probed yet"}
        self._task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        return {"plugin": self.label, **self._last}

    async def probe_once(self) -> dict[str, Any]:
        try:
            result = dict(await self.plugin.probe())
        except Exception as e:
            result = {"ok": False, "message": f"{type(e).__name__}: {e}"}
        result["ok"] = bool(result.get("ok", False))
        result.setdefault("message", "")
        previous = self._last
        self._last = result
        if self.plugin.name == NONE_NAME:
            if previous.get("message") == "not probed yet":
                logger.info("TTS plugin: %s (off)", self.label)
        elif previous.get("ok") != result["ok"] or previous.get("message") == "not probed yet":
            if result["ok"]:
                logger.info("TTS plugin: %s ok (%s)", self.label, result["message"])
            else:
                logger.warning("TTS plugin: %s NG (%s)", self.label, result["message"])
        return result

    async def _loop(self) -> None:
        while True:
            await self.probe_once()
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
