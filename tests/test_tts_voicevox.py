"""voicevox プラグイン (httpx.MockTransport)。"""

import io
import json
import wave

import httpx
import numpy as np
import pytest

from openmovehub.plugins.tts_voicevox import VoicevoxTts, create, parse_wav

SPEAKERS = [
    {"name": "四国めたん", "styles": [{"name": "ノーマル", "id": 2}, {"name": "あまあま", "id": 0}]},
    {"name": "ずんだもん", "styles": [{"name": "ノーマル", "id": 3}, {"name": "あまあま", "id": 1}]},
]


def make_wav(rate: int = 24000, samples: int = 240, channels: int = 1, sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        data = (np.arange(samples * channels) % 100).astype(np.int16 if sampwidth == 2 else np.uint8)
        wf.writeframes(data.tobytes())
    return buf.getvalue()


def make_transport(wav: bytes, requests: list[httpx.Request] | None = None, version_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path == "/audio_query":
            return httpx.Response(200, json={"accent_phrases": [], "speedScale": 1.0, "outputSamplingRate": 24000})
        if request.url.path == "/synthesis":
            return httpx.Response(200, content=wav, headers={"Content-Type": "audio/wav"})
        if request.url.path == "/version":
            return httpx.Response(version_status, json="0.25.2")
        if request.url.path == "/speakers":
            return httpx.Response(200, json=SPEAKERS)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_synthesize_reads_rate_from_wav_header():
    requests: list[httpx.Request] = []
    wav = make_wav(rate=24000, samples=240)
    tts = VoicevoxTts(speaker=1, speed=1.2, transport=make_transport(wav, requests))
    audio = await tts.synthesize("こんにちは")
    await tts.close()
    assert audio.sample_rate == 24000
    assert len(audio.pcm) == 240 * 2
    query_req, synth_req = requests
    assert query_req.url.path == "/audio_query"
    assert query_req.url.params["text"] == "こんにちは"
    assert query_req.url.params["speaker"] == "1"
    assert synth_req.url.path == "/synthesis"
    assert synth_req.url.params["speaker"] == "1"
    assert json.loads(synth_req.content)["speedScale"] == 1.2


async def test_synthesize_other_rate_and_stereo_downmix():
    tts = VoicevoxTts(transport=make_transport(make_wav(rate=48000, samples=100, channels=2)))
    audio = await tts.synthesize("x")
    await tts.close()
    assert audio.sample_rate == 48000
    assert len(audio.pcm) == 100 * 2


async def test_synthesize_invalid_wav_raises():
    tts = VoicevoxTts(transport=make_transport(b"not a wav at all"))
    with pytest.raises(ValueError, match="invalid WAV"):
        await tts.synthesize("x")
    await tts.close()


def test_parse_wav_rejects_8bit():
    with pytest.raises(ValueError, match="sample width"):
        parse_wav(make_wav(sampwidth=1))


async def test_synthesize_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    tts = VoicevoxTts(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await tts.synthesize("x")
    await tts.close()


async def test_probe_reports_version_and_speaker_name():
    tts = VoicevoxTts(speaker=2, transport=make_transport(make_wav()))
    result = await tts.probe()
    await tts.close()
    assert result["ok"] is True
    assert result["message"] == "version 0.25.2"
    assert result["version"] == "0.25.2"
    assert result["speaker"] == 2
    assert (result["speaker_name"], result["style_name"]) == ("四国めたん", "ノーマル")


async def test_probe_unknown_speaker_id_has_no_name():
    tts = VoicevoxTts(speaker=999, transport=make_transport(make_wav()))
    result = await tts.probe()
    await tts.close()
    assert result["ok"] is True
    assert result["speaker_name"] is None


async def test_probe_engine_down_is_not_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    tts = VoicevoxTts(transport=httpx.MockTransport(handler))
    result = await tts.probe()
    await tts.close()
    assert result["ok"] is False
    assert "ConnectError" in result["message"]


def test_create_reads_env(monkeypatch):
    monkeypatch.setenv("VOICEVOX_URL", "http://example:1234")
    monkeypatch.setenv("VOICEVOX_SPEAKER", "3")
    monkeypatch.setenv("VOICEVOX_SPEED", "1.5")
    tts = create(None)
    assert (tts.url, tts.speaker, tts.speed) == ("http://example:1234", 3, 1.5)
