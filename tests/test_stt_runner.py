"""SttRunner (バックグラウンドロード・状態・ready 前破棄・NG ワード・heard フック)。"""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock

from openmovehub.plugins.none import NoneStt
from openmovehub.services.stt_runner import SttRunner, load_ng_words


class FakeStt:
    name = "fake"

    def __init__(self, text: str = "こんにちは", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.release = threading.Event()
        self.release.set()
        self.load_calls = 0
        self.calls: list[tuple[bytes, int]] = []
        self.unloaded = False

    def load(self) -> None:
        self.load_calls += 1
        self.release.wait(5)
        if self.fail:
            raise RuntimeError("no model")

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls.append((pcm, sample_rate))
        return self.text

    def unload(self) -> None:
        self.unloaded = True

    def status(self) -> dict[str, Any]:
        return {"model": "fake-model"}


async def _wait_state(runner: SttRunner, state: str) -> None:
    for _ in range(200):
        if runner.state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"state stayed {runner.state}")


async def test_loading_then_ready_and_notifies():
    plugin = FakeStt()
    plugin.release.clear()
    notify = AsyncMock()
    runner = SttRunner(plugin, notify=notify)
    assert runner.state == "loading"
    runner.start()
    runner.start()  # 2 回目は何もしない
    assert runner.status() == {"plugin": "fake", "state": "loading", "model": "fake-model"}
    plugin.release.set()
    await _wait_state(runner, "ready")
    await asyncio.sleep(0.01)
    assert plugin.load_calls == 1
    notify.assert_awaited_once_with("ready")


async def test_utterances_before_ready_are_dropped():
    plugin = FakeStt()
    plugin.release.clear()
    runner = SttRunner(plugin)
    runner.start()
    assert await runner.transcribe(b"\x00\x00" * 100, 16000) == ""
    assert plugin.calls == []
    assert runner.dropped_not_ready == 1
    plugin.release.set()
    await _wait_state(runner, "ready")
    assert await runner.transcribe(b"\x01\x00" * 100, 16000) == "こんにちは"
    assert plugin.calls == [(b"\x01\x00" * 100, 16000)]


async def test_load_exception_sets_error():
    notify = AsyncMock()
    runner = SttRunner(FakeStt(fail=True), label="fake", notify=notify)
    runner.start()
    await _wait_state(runner, "error")
    await asyncio.sleep(0.01)
    status = runner.status()
    assert status["state"] == "error"
    assert status["error"] == "RuntimeError: no model"
    notify.assert_awaited_once_with("error")
    assert await runner.transcribe(b"\x00\x00", 16000) == ""


async def test_none_plugin_is_off_and_never_loads():
    runner = SttRunner(NoneStt(), label="none")
    assert runner.state == "off"
    assert runner.accepts_audio is False
    runner.start()
    assert runner._thread is None
    assert runner.status() == {"plugin": "none", "state": "off"}


async def test_fallback_label_is_reported():
    runner = SttRunner(NoneStt(), label="none (fallback: LookupError: unknown stt plugin: 'nosuch')")
    assert runner.status()["plugin"].startswith("none (fallback: ")


async def test_ng_words_are_filtered(tmp_path):
    ng = tmp_path / "ng.txt"
    ng.write_text("# comment\nご視聴ありがとうございました\n\n", encoding="utf-8")
    words = load_ng_words(str(ng))
    assert words == {"ご視聴ありがとうございました"}
    plugin = FakeStt(text=" ご視聴ありがとうございました ")
    runner = SttRunner(plugin, ng_words=words)
    runner.start()
    await _wait_state(runner, "ready")
    assert await runner.transcribe(b"\x00\x00", 16000) == ""
    plugin.text = "ご視聴ありがとうございました。今日は"
    assert await runner.transcribe(b"\x00\x00", 16000) == "ご視聴ありがとうございました。今日は"


def test_missing_ng_words_file_disables_filter(tmp_path):
    assert load_ng_words(str(tmp_path / "missing.txt")) == set()


async def test_heard_hook_default_is_empty_intent():
    runner = SttRunner(FakeStt())
    assert await runner.on_heard("こんにちは") == ""


async def test_heard_hook_returns_intent_and_survives_errors():
    runner = SttRunner(FakeStt())
    runner.heard_hook = AsyncMock(return_value="vlm:ask")
    assert await runner.on_heard("これは何") == "vlm:ask"
    runner.heard_hook.assert_awaited_once_with("これは何")
    runner.heard_hook = AsyncMock(side_effect=RuntimeError("boom"))
    assert await runner.on_heard("これは何") == ""


async def test_close_unloads_only_when_ready():
    plugin = FakeStt()
    runner = SttRunner(plugin)
    runner.close()
    assert plugin.unloaded is False
    runner.start()
    await _wait_state(runner, "ready")
    runner.close()
    assert plugin.unloaded is True
