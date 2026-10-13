"""``POST /speak``・``GET /audio/plugins``・``/status.audio`` と TtsMonitor。"""

import logging
from unittest.mock import AsyncMock, MagicMock

from openmovehub.plugins.none import NoneStt, NoneTts
from openmovehub.services.stt_runner import SttRunner
from openmovehub.services.tts_monitor import TtsMonitor
from tests.test_rest import build_test_app, client, make_mock_manager


class FakeTts:
    name = "voicevox"

    def __init__(self, results):
        self.results = list(results)

    async def probe(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def test_post_speak_enqueues():
    app = build_test_app(make_mock_manager())
    app.state.speech_queue = MagicMock(enqueue=AsyncMock())
    async with client(app) as c:
        response = await c.post("/speak", json={"text": "音声出力の確認です"})
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    app.state.speech_queue.enqueue.assert_awaited_once_with("音声出力の確認です", None)


async def test_post_speak_requires_text():
    app = build_test_app(make_mock_manager())
    app.state.speech_queue = MagicMock(enqueue=AsyncMock())
    async with client(app) as c:
        response = await c.post("/speak", json={})
    assert response.status_code == 422


async def test_status_audio_reports_plugins_and_states():
    app = build_test_app(make_mock_manager())
    plugin = MagicMock()
    plugin.name = "whisper"
    plugin.status = MagicMock(return_value={"model": "large-v3-turbo", "device": "cuda"})
    runner = SttRunner(plugin, label="whisper")
    runner._state = "ready"
    app.state.stt_runner = runner
    monitor = TtsMonitor(FakeTts([{"ok": True, "message": "version 0.25.2", "speaker_name": "ずんだもん"}]))
    await monitor.probe_once()
    app.state.tts_monitor = monitor
    async with client(app) as c:
        audio = (await c.get("/status")).json()["audio"]
    assert audio == {
        "stt": {"plugin": "whisper", "state": "ready", "model": "large-v3-turbo", "device": "cuda"},
        "tts": {"plugin": "voicevox", "ok": True, "message": "version 0.25.2", "speaker_name": "ずんだもん"},
    }


async def test_status_audio_none_and_fallback():
    app = build_test_app(make_mock_manager())
    app.state.stt_runner = SttRunner(NoneStt(), label="none (fallback: LookupError: unknown stt plugin: 'nosuch')")
    monitor = TtsMonitor(NoneTts(), label="none")
    await monitor.probe_once()
    app.state.tts_monitor = monitor
    async with client(app) as c:
        audio = (await c.get("/status")).json()["audio"]
    assert audio["stt"]["state"] == "off"
    assert audio["stt"]["plugin"].startswith("none (fallback: ")
    assert audio["tts"] == {"plugin": "none", "ok": False, "message": "disabled"}


async def test_audio_plugins_lists_builtins_and_selection():
    app = build_test_app(make_mock_manager())
    app.state.stt_runner = SttRunner(NoneStt(), label="none")
    async with client(app) as c:
        body = (await c.get("/audio/plugins")).json()
    assert {"whisper", "none"} <= set(body["stt"])
    assert {"voicevox", "none"} <= set(body["tts"])
    assert body["selected"] == {"stt": "none", "tts": "none"}


async def test_tts_monitor_logs_only_on_change(caplog):
    caplog.set_level(logging.INFO, logger="openmovehub.services.tts_monitor")
    monitor = TtsMonitor(
        FakeTts(
            [
                {"ok": True, "message": "version 0.25.2"},
                {"ok": True, "message": "version 0.25.2"},
                ConnectionError("refused"),
                {"ok": True, "message": "version 0.25.2"},
            ]
        )
    )
    for _ in range(4):
        await monitor.probe_once()
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "TTS plugin: voicevox ok (version 0.25.2)",
        "TTS plugin: voicevox NG (ConnectionError: refused)",
        "TTS plugin: voicevox ok (version 0.25.2)",
    ]
    assert monitor.status() == {"plugin": "voicevox", "ok": True, "message": "version 0.25.2"}


async def test_tts_monitor_loop_probes_periodically():
    tts = FakeTts([{"ok": False, "message": "down"}] + [{"ok": True, "message": "version 1"}] * 10)
    monitor = TtsMonitor(tts, interval_s=0.01)
    monitor.start()
    import asyncio

    for _ in range(100):
        if monitor.status()["ok"]:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()
    assert monitor.status()["ok"] is True
