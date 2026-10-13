"""Silero VAD (ONNX、CPU)。stackchan_move ``services/vad.py`` (commit a6e8234) と同一。

モデルは ``silero-vad`` パッケージに同梱の ``silero_vad/data/silero_vad.onnx`` を onnxruntime で読む
(``silero_vad`` モジュール自体は import しないので torch は不要)。
"""

import importlib.util
import logging
from collections import deque
from enum import Enum
from pathlib import Path

import numpy as np
import onnxruntime as ort

from openmovehub.config import Settings

logger = logging.getLogger(__name__)

FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
STATE_DIM = 128


class _State(Enum):
    IDLE = "idle"
    SPEAKING = "speaking"
    TRAILING = "trailing"


class VADService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        spec = importlib.util.find_spec("silero_vad")
        if spec is None or spec.submodule_search_locations is None:
            raise RuntimeError("silero-vad package not installed")
        model_path = Path(spec.submodule_search_locations[0]) / "data" / "silero_vad.onnx"
        if not model_path.exists():
            raise RuntimeError(f"Silero VAD ONNX model not found: {model_path}")

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        logger.info("Silero VAD ONNX model loaded from %s", model_path)

    def create_accumulator(self) -> "VadAccumulator":
        return VadAccumulator(self._session, self._settings)


class VadAccumulator:
    def __init__(self, session: ort.InferenceSession, settings: Settings) -> None:
        self._session = session
        self._threshold = settings.vad_threshold
        self._sample_rate = settings.sample_rate

        ms_per_frame = FRAME_SAMPLES * 1000 / self._sample_rate
        self._min_silence_frames = max(int(settings.vad_min_silence_ms / ms_per_frame), 1)
        self._max_speech_samples = settings.vad_max_speech_s * self._sample_rate

        prebuffer_frames = max(int(settings.vad_speech_pad_ms / ms_per_frame), 1)
        self._prebuffer: deque[bytes] = deque(maxlen=prebuffer_frames)

        self._onnx_state = np.zeros((2, 1, STATE_DIM), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

        self._fsm = _State.IDLE
        self._speech_buf: bytearray = bytearray()
        self._silence_frames = 0
        self._speech_samples = 0
        self._residual = b""

    def _infer(self, frame_f32: np.ndarray) -> float:
        x = np.concatenate([self._context, frame_f32.reshape(1, -1)], axis=1)
        ort_inputs = {
            "input": x,
            "state": self._onnx_state,
            "sr": np.array(self._sample_rate, dtype=np.int64),
        }
        out, state_n = self._session.run(None, ort_inputs)
        self._onnx_state = state_n
        self._context = x[:, -CONTEXT_SAMPLES:]
        return float(out.squeeze())

    def feed(self, chunk: bytes) -> bytes | None:
        data = self._residual + chunk
        self._residual = b""

        samples = np.frombuffer(data, dtype=np.int16)
        n = len(samples)

        pos = 0
        while pos + FRAME_SAMPLES <= n:
            frame_int16 = samples[pos : pos + FRAME_SAMPLES]
            frame_bytes = frame_int16.tobytes()
            frame_f32 = frame_int16.astype(np.float32) / 32768.0

            prob = self._infer(frame_f32)
            result = self._step(prob, frame_bytes)
            if result is not None:
                remaining = pos + FRAME_SAMPLES
                if remaining < n:
                    self._residual = samples[remaining:].tobytes()
                return result

            pos += FRAME_SAMPLES

        if pos < n:
            self._residual = samples[pos:].tobytes()

        return None

    def _step(self, prob: float, frame_bytes: bytes) -> bytes | None:
        if self._fsm == _State.IDLE:
            self._prebuffer.append(frame_bytes)
            if prob >= self._threshold:
                logger.debug("Speech start (prob=%.3f)", prob)
                self._fsm = _State.SPEAKING
                self._speech_buf = bytearray()
                for buf in self._prebuffer:
                    self._speech_buf.extend(buf)
                self._prebuffer.clear()
                self._speech_samples = len(self._speech_buf) // 2
                self._silence_frames = 0
            return None

        self._speech_buf.extend(frame_bytes)
        self._speech_samples += FRAME_SAMPLES

        if self._speech_samples >= self._max_speech_samples:
            logger.debug("Max speech duration reached, forcing return")
            return self._finish()

        if self._fsm == _State.SPEAKING:
            if prob < self._threshold:
                self._fsm = _State.TRAILING
                self._silence_frames = 1
            return None

        if prob >= self._threshold:
            self._fsm = _State.SPEAKING
            self._silence_frames = 0
            return None

        self._silence_frames += 1
        if self._silence_frames >= self._min_silence_frames:
            logger.debug("Speech end (silence_frames=%d)", self._silence_frames)
            return self._finish()

        return None

    def _finish(self) -> bytes:
        utterance = bytes(self._speech_buf)
        self._speech_buf = bytearray()
        self._speech_samples = 0
        self._silence_frames = 0
        self._fsm = _State.IDLE
        return utterance
