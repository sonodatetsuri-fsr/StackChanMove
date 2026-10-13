"""whisper プラグイン (auto 判定・cuda 失敗時の cpu 再試行)。実モデルはロードしない。"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from openmovehub.plugins import stt_whisper
from openmovehub.plugins.stt_whisper import WhisperStt, add_windows_dll_directories, create, resolve_device


def _segments(*texts: str):
    return [MagicMock(text=t) for t in texts]


def _fake_model(texts=("こんにちは",)):
    model = MagicMock()
    model.transcribe = MagicMock(return_value=(_segments(*texts), None))
    return model


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, ("cuda", "float16")), (0, ("cpu", "int8"))],
)
def test_auto_resolves_by_cuda_device_count(count, expected):
    with patch.object(stt_whisper.ctranslate2, "get_cuda_device_count", return_value=count):
        assert resolve_device("auto", "auto") == expected


def test_auto_device_with_explicit_compute_type():
    with patch.object(stt_whisper.ctranslate2, "get_cuda_device_count", return_value=0):
        assert resolve_device("auto", "float32") == ("cpu", "float32")


def test_explicit_device_skips_detection():
    with patch.object(stt_whisper.ctranslate2, "get_cuda_device_count", side_effect=AssertionError):
        assert resolve_device("cuda", "auto") == ("cuda", "float16")
        assert resolve_device("cpu", "auto") == ("cpu", "int8")


def test_cuda_detection_error_falls_back_to_cpu():
    with patch.object(stt_whisper.ctranslate2, "get_cuda_device_count", side_effect=RuntimeError("driver")):
        assert resolve_device("auto", "auto") == ("cpu", "int8")


def test_create_reads_env(monkeypatch):
    monkeypatch.setenv("STT_MODEL", "small")
    monkeypatch.setenv("STT_DEVICE", "cpu")
    monkeypatch.delenv("STT_COMPUTE_TYPE", raising=False)
    monkeypatch.setenv("STT_LANGUAGE", "en")
    plugin = create(None)
    assert plugin.status() == {"model": "small", "device": "cpu", "compute_type": "int8", "language": "en"}


def test_cuda_load_success_runs_warmup():
    model = _fake_model()
    with patch.object(stt_whisper, "WhisperModel", return_value=model) as ctor:
        plugin = WhisperStt(model="m", device="cuda", compute_type="auto")
        plugin.load()
    ctor.assert_called_once_with("m", device="cuda", compute_type="float16")
    model.transcribe.assert_called_once()  # 1 s の無音での試し認識
    assert plugin.status()["device"] == "cuda"
    assert "cuda_fallback" not in plugin.status()


def test_cuda_construct_failure_retries_on_cpu_once():
    cpu_model = _fake_model()
    calls = []

    def ctor(name, device, compute_type):
        calls.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
        return cpu_model

    with patch.object(stt_whisper, "WhisperModel", side_effect=ctor):
        plugin = WhisperStt(model="m", device="cuda", compute_type="auto")
        plugin.load()
    assert calls == [("cuda", "float16"), ("cpu", "int8")]
    status = plugin.status()
    assert (status["device"], status["compute_type"]) == ("cpu", "int8")
    assert "libcublas" in status["cuda_fallback"]
    assert plugin._model is cpu_model


def test_cuda_warmup_failure_retries_on_cpu():
    cuda_model = MagicMock()
    cuda_model.transcribe = MagicMock(side_effect=RuntimeError("cudnn missing"))
    cpu_model = _fake_model()
    with patch.object(stt_whisper, "WhisperModel", side_effect=[cuda_model, cpu_model]):
        plugin = WhisperStt(device="cuda")
        plugin.load()
    assert plugin._model is cpu_model
    assert plugin.status()["device"] == "cpu"


def test_cpu_load_failure_is_raised_without_retry():
    with patch.object(stt_whisper, "WhisperModel", side_effect=RuntimeError("download failed")) as ctor:
        plugin = WhisperStt(device="cpu")
        with pytest.raises(RuntimeError, match="download failed"):
            plugin.load()
    assert ctor.call_count == 1


def test_cuda_and_cpu_both_fail_raises():
    with patch.object(stt_whisper, "WhisperModel", side_effect=RuntimeError("nope")) as ctor:
        plugin = WhisperStt(device="cuda")
        with pytest.raises(RuntimeError):
            plugin.load()
    assert ctor.call_count == 2


def test_transcribe_converts_pcm_to_float_and_joins_segments():
    model = _fake_model(("こんにちは、", "今日は"))
    with patch.object(stt_whisper, "WhisperModel", return_value=model):
        plugin = WhisperStt(device="cpu", language="ja")
        plugin.load()
    pcm = np.array([0, 16384, -32768], dtype=np.int16).tobytes()
    assert plugin.transcribe(pcm, 16000) == "こんにちは、今日は"
    audio = model.transcribe.call_args.args[0]
    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, [0.0, 0.5, -1.0])
    assert model.transcribe.call_args.kwargs == {"language": "ja"}


def test_transcribe_resamples_non_16k_input():
    model = _fake_model()
    with patch.object(stt_whisper, "WhisperModel", return_value=model):
        plugin = WhisperStt(device="cpu")
        plugin.load()
    plugin.transcribe(b"\x00\x00" * 480, 48000)
    assert len(model.transcribe.call_args.args[0]) == 160


def test_transcribe_before_load_and_after_unload_is_empty():
    plugin = WhisperStt(device="cpu")
    assert plugin.transcribe(b"\x00\x00", 16000) == ""
    with patch.object(stt_whisper, "WhisperModel", return_value=_fake_model()):
        plugin.load()
    plugin.unload()
    assert plugin.transcribe(b"\x00\x00", 16000) == ""


def test_windows_dll_directories_noop_on_linux(monkeypatch):
    monkeypatch.setattr(stt_whisper.sys, "platform", "linux")
    assert add_windows_dll_directories() == []


def test_windows_dll_directories_registers_nvidia_bins(monkeypatch, tmp_path):
    for sub in ("cublas", "cudnn"):
        (tmp_path / "nvidia" / sub / "bin").mkdir(parents=True)
    monkeypatch.setattr(stt_whisper.sys, "platform", "win32")
    monkeypatch.setattr(
        stt_whisper, "_nvidia_bin_dirs", lambda: [tmp_path / "nvidia" / s / "bin" for s in ("cublas", "cudnn")]
    )
    added_dirs = []
    monkeypatch.setattr(stt_whisper.os, "add_dll_directory", lambda d: added_dirs.append(d) or object(), raising=False)
    monkeypatch.setenv("PATH", "orig")
    added = add_windows_dll_directories()
    assert len(added) == 2
    assert added_dirs == [str(p) for p in added]
    assert stt_whisper.os.environ["PATH"].endswith("orig")
