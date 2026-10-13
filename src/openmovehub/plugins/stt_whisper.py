"""``whisper`` STT プラグイン (faster-whisper)。

| env | 既定 | 説明 |
|---|---|---|
| ``STT_MODEL`` | ``large-v3-turbo`` | faster-whisper のモデル名またはローカルパス |
| ``STT_DEVICE`` | ``auto`` | ``auto`` / ``cuda`` / ``cpu`` (``auto`` は CUDA デバイスがあれば ``cuda``) |
| ``STT_COMPUTE_TYPE`` | ``auto`` | ``auto`` は ``cuda`` なら ``float16``、``cpu`` なら ``int8`` |
| ``STT_LANGUAGE`` | ``ja`` | |

``cuda`` でのロード (と、CUDA ライブラリを実際に読み込ませる 1 s の無音での試し認識) が失敗したら
警告を出して ``cpu`` / ``int8`` で 1 回だけロードし直す。

Windows では import 前に、pip パッケージ ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` (依存グループ
``cuda``) の ``bin`` を ``os.add_dll_directory()`` で登録する。
"""

import gc
import logging
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: ``add_dll_directory`` の戻り値 (解放されると登録が外れるので保持する)。
_DLL_HANDLES: list[Any] = []


def _nvidia_bin_dirs() -> list[Path]:
    """pip の ``nvidia-*`` パッケージ (namespace ``nvidia``) の ``bin`` ディレクトリ。"""
    import importlib.util

    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        return []
    if spec is None or not spec.submodule_search_locations:
        return []
    dirs: list[Path] = []
    for root in spec.submodule_search_locations:
        for sub in ("cublas", "cudnn"):
            candidate = Path(root) / sub / "bin"
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def add_windows_dll_directories() -> list[Path]:
    """Windows のときだけ cuBLAS / cuDNN の DLL ディレクトリを登録する (それ以外は何もしない)。"""
    if sys.platform != "win32":
        return []
    added: list[Path] = []
    for directory in _nvidia_bin_dirs():
        try:
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))  # type: ignore[attr-defined]
        except OSError:
            logger.warning("os.add_dll_directory failed: %s", directory)
            continue
        # CTranslate2 は LoadLibrary で読むため PATH にも足す。
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        added.append(directory)
    if added:
        logger.info("Registered CUDA DLL directories: %s", ", ".join(str(d) for d in added))
    return added


add_windows_dll_directories()

import ctranslate2  # noqa: E402  (DLL ディレクトリ登録の後に import する)
from faster_whisper import WhisperModel  # noqa: E402

#: CUDA ライブラリの読み込みを確かめる試し認識の長さ [サンプル@16 kHz]。
_WARMUP_SAMPLES = 16000


def resolve_device(device: str, compute_type: str) -> tuple[str, str]:
    """``auto`` を解決して ``(device, compute_type)`` を返す。"""
    device = (device or "auto").strip().lower()
    if device == "auto":
        try:
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            logger.warning("ctranslate2.get_cuda_device_count() failed; using cpu", exc_info=True)
            device = "cpu"
    compute_type = (compute_type or "auto").strip().lower()
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


class WhisperStt:
    name = "whisper"

    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "auto",
        compute_type: str = "auto",
        language: str = "ja",
    ) -> None:
        self.model_name = model
        self.requested_device = device
        self.requested_compute_type = compute_type
        self.language = language
        self.device, self.compute_type = resolve_device(device, compute_type)
        self.fallback: str | None = None
        self._model: Any = None

    def _construct(self) -> Any:
        model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        if self.device == "cuda":
            # CTranslate2 は cuBLAS / cuDNN を最初の推論で読み込むため、ここで失敗させて CPU 再試行に回す。
            segments, _ = model.transcribe(np.zeros(_WARMUP_SAMPLES, dtype=np.float32), language=self.language)
            for _ in segments:
                pass
        return model

    def load(self) -> None:
        try:
            self._model = self._construct()
        except Exception as e:
            if self.device != "cuda":
                raise
            reason = f"{type(e).__name__}: {e}"
            logger.warning("STT cuda load failed (%s); retrying on cpu/int8", reason)
            self._model = None
            gc.collect()
            self.fallback = reason
            self.device, self.compute_type = "cpu", "int8"
            self._model = self._construct()

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        model = self._model
        if model is None:
            return ""
        if sample_rate != 16000:
            from openmovehub.services.resample import resample_pcm16

            pcm = resample_pcm16(pcm, sample_rate, 16000)
        pcm_int16 = np.frombuffer(pcm, dtype=np.int16)
        pcm_float32 = pcm_int16.astype(np.float32) / 32768.0
        segments, _ = model.transcribe(pcm_float32, language=self.language)
        return "".join(segment.text for segment in segments)

    def unload(self) -> None:
        self._model = None
        gc.collect()

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "model": self.model_name,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language,
        }
        if self.fallback is not None:
            status["cuda_fallback"] = self.fallback
        return status


def create(settings: Any = None) -> WhisperStt:
    return WhisperStt(
        model=os.getenv("STT_MODEL", "large-v3-turbo"),
        device=os.getenv("STT_DEVICE", "auto"),
        compute_type=os.getenv("STT_COMPUTE_TYPE", "auto"),
        language=os.getenv("STT_LANGUAGE", "ja"),
    )
