"""(stackchan_move から移植) VadAccumulator state machine logic.

Mock the ONNX inference to test state transitions (IDLE -> SPEAKING -> TRAILING -> IDLE),
pre-buffer inclusion, residual sample handling, and max-speech forced return.
"""

from unittest.mock import MagicMock, patch

import numpy as np

from openmovehub.config import Settings
from openmovehub.services.vad import VADService

FRAME_SIZE = 512
BYTES_PER_SAMPLE = 2


def make_pcm16_chunk(n_samples: int, value: int = 0) -> bytes:
    """Create a PCM16 audio chunk with n_samples samples."""
    return np.full(n_samples, value, dtype=np.int16).tobytes()


def _build_accumulator(probs, *, settings=None):
    """Build a VadAccumulator backed by a mocked ONNX session.

    *probs* is an iterable of per-frame speech probabilities (0.0-1.0).
    After exhaustion, every subsequent frame returns 0.0.

    Returns (mock_session, accumulator) so tests can inspect call counts.
    """
    if settings is None:
        settings = Settings()

    prob_iter = iter(probs)
    mock_session = MagicMock()

    def _run(_output_names, _input_feed):
        try:
            p = next(prob_iter)
        except StopIteration:
            p = 0.0
        return [
            np.array([[p]], dtype=np.float32),
            np.zeros((2, 1, 128), dtype=np.float32),
        ]

    mock_session.run.side_effect = _run

    with patch("openmovehub.services.vad.ort.InferenceSession", return_value=mock_session):
        service = VADService(settings)

    return mock_session, service.create_accumulator()


def _make_accumulator(probs, *, settings=None):
    """Convenience wrapper returning only the accumulator."""
    _, acc = _build_accumulator(probs, settings=settings)
    return acc


# ---- Tests ----


def test_silence_returns_none():
    frames_per_chunk = 16000 // FRAME_SIZE  # 31
    probs = [0.0] * frames_per_chunk
    acc = _make_accumulator(probs)

    chunk = make_pcm16_chunk(16000)
    result = acc.feed(chunk)

    assert result is None


def test_speech_then_silence_returns_utterance():
    speech_frames = 31
    silence_frames = 31
    probs = [0.9] * speech_frames + [0.0] * silence_frames
    acc = _make_accumulator(probs)

    speech_chunk = make_pcm16_chunk(16000, value=1000)
    result = acc.feed(speech_chunk)
    assert result is None

    silence_chunk = make_pcm16_chunk(16000, value=0)
    result = acc.feed(silence_chunk)
    assert result is not None
    assert len(result) > 0


def test_speech_continues_returns_none():
    probs = [0.9] * 62
    acc = _make_accumulator(probs)

    chunk1 = make_pcm16_chunk(16000, value=1000)
    assert acc.feed(chunk1) is None

    chunk2 = make_pcm16_chunk(16000, value=1000)
    assert acc.feed(chunk2) is None


def test_trailing_speech_resumes():
    speech1_frames = 20
    brief_silence_frames = 10  # 10 * 32ms = 320ms < 800ms min_silence
    speech2_frames = 20
    probs = [0.9] * speech1_frames + [0.0] * brief_silence_frames + [0.9] * speech2_frames
    acc = _make_accumulator(probs)

    chunk = make_pcm16_chunk(
        (speech1_frames + brief_silence_frames + speech2_frames) * FRAME_SIZE,
        value=1000,
    )
    result = acc.feed(chunk)
    assert result is None


def test_max_speech_forced_return():
    settings = Settings(vad_max_speech_s=1)
    probs = [0.9] * 200
    acc = _make_accumulator(probs, settings=settings)

    results = []
    for _ in range(3):
        chunk = make_pcm16_chunk(16000, value=1000)
        r = acc.feed(chunk)
        if r is not None:
            results.append(r)

    assert len(results) >= 1


def test_prebuffer_included():
    idle_frames = 20
    speech_frames = 10
    silence_frames = 30
    probs = [0.0] * idle_frames + [0.9] * speech_frames + [0.0] * silence_frames
    acc = _make_accumulator(probs)

    idle_audio = make_pcm16_chunk(idle_frames * FRAME_SIZE, value=42)
    assert acc.feed(idle_audio) is None

    speech_audio = make_pcm16_chunk(speech_frames * FRAME_SIZE, value=1000)
    assert acc.feed(speech_audio) is None

    silence_audio = make_pcm16_chunk(silence_frames * FRAME_SIZE, value=0)
    result = acc.feed(silence_audio)

    assert result is not None
    speech_only_bytes = speech_frames * FRAME_SIZE * BYTES_PER_SAMPLE
    assert len(result) > speech_only_bytes

    arr = np.frombuffer(result, dtype=np.int16)
    assert 42 in arr


def test_residual_samples_carried_over():
    probs = [0.0] * 10
    mock_session, acc = _build_accumulator(probs)

    chunk1 = make_pcm16_chunk(1000)
    acc.feed(chunk1)
    calls_after_first = mock_session.run.call_count
    assert calls_after_first == 1  # 1000 // 512 = 1 frame, 488 residual

    chunk2 = make_pcm16_chunk(1000)
    acc.feed(chunk2)
    calls_after_second = mock_session.run.call_count
    assert calls_after_second == 3  # (488 + 1000) // 512 = 2 more frames


def test_state_reset_after_return():
    speech_frames = 10
    silence_frames = 30
    post_silence_frames = 31
    probs = [0.9] * speech_frames + [0.0] * silence_frames + [0.0] * post_silence_frames
    acc = _make_accumulator(probs)

    speech_chunk = make_pcm16_chunk(speech_frames * FRAME_SIZE, value=1000)
    acc.feed(speech_chunk)

    silence_chunk = make_pcm16_chunk(silence_frames * FRAME_SIZE, value=0)
    result = acc.feed(silence_chunk)
    assert result is not None

    post_silence_chunk = make_pcm16_chunk(post_silence_frames * FRAME_SIZE, value=0)
    result = acc.feed(post_silence_chunk)
    assert result is None
