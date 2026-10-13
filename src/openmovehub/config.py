"""Hub の設定 (環境変数と ``.env``)。

読み方は stackchan_move の ``config.py`` を踏襲する (``python-dotenv`` で ``.env`` を読み、
``os.getenv`` で既定値を補う)。

VLM (``VLM_*``、stackchan_move と同じ名前・既定) と、その上の質問応答 (``VLM_QA_*``)・
音声入出力の既定 (``VLM_VOICE_*_DEFAULT``) をここに置く。

音声の Hub 共通設定 (``SAMPLE_RATE``・``VAD_*``・``SPEAK_*``・``STT_NG_WORDS_PATH``) と
プラグインの選択 (``STT_PLUGIN`` / ``TTS_PLUGIN``) をここに置く。プラグイン固有の設定
(``STT_MODEL``・``VOICEVOX_URL`` など) は各プラグインが自分で env から読む。
"""

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

DriveSourceSetting = Literal["hub", "joystick"]
VlmBackendSetting = Literal["ollama", "none"]

#: 質問応答の既定のシステムプロンプト (``VLM_QA_SYSTEM_PROMPT`` で差し替え可)。
DEFAULT_VLM_QA_SYSTEM_PROMPT = (
    "あなたは小型ロボットの目です。添付画像はロボットのカメラが今写している映像です。"
    "質問に、画像に写っているものだけを根拠に日本語で答えてください。"
    "音声で読み上げられるので、2〜3 文以内の話し言葉で、箇条書きや記号を使わずに答えてください。"
    "画像から分からないことは分からないと答えてください。"
)

#: odom 送信レートの値域 [Hz] (ファームの ``ODOM_HZ_MIN`` / ``ODOM_HZ_MAX``、stackchan_move と同じ)。
ODOM_HZ_MIN = 5
ODOM_HZ_MAX = 50


def clamp_odom_hz(value: int) -> int:
    """odom 送信レートを 5..50 Hz に丸める。"""
    return max(ODOM_HZ_MIN, min(ODOM_HZ_MAX, int(value)))


def _env_flag(name: str, default: str = "on") -> bool:
    return os.getenv(name, default).strip().lower() not in ("off", "false", "0")


#: 非推奨の env を使ったときの警告 (ロガー設定後に ``main`` の lifespan が出す)。
DEPRECATION_WARNINGS: list[str] = []


def _env_stt_plugin() -> str:
    """``STT_PLUGIN`` (既定 ``whisper``)。``HUB_STT_ENABLED=off`` は ``none`` として扱う (非推奨)。"""
    plugin = os.getenv("STT_PLUGIN", "").strip()
    if not _env_flag("HUB_STT_ENABLED"):
        DEPRECATION_WARNINGS.append(
            "HUB_STT_ENABLED=off is deprecated; use STT_PLUGIN=none (treated as STT_PLUGIN=none)"
        )
        return "none"
    return plugin or "whisper"


def _env_vlm_backend(name: str, default: str = "ollama") -> VlmBackendSetting:
    """``ollama`` / ``none``。未知の値は ``none`` 扱い (警告は ``main`` が出す)。"""
    value = os.getenv(name, default).strip().lower()
    if value not in ("ollama", "none"):
        return "none"
    return value  # type: ignore[return-value]


def _env_drive_source(name: str, default: str = "hub") -> DriveSourceSetting:
    """``hub`` / ``joystick`` 以外 (``auto`` を含む) は ``hub`` にする。"""
    value = os.getenv(name, default).strip().lower()
    if value not in ("hub", "joystick"):
        return "hub"
    return value  # type: ignore[return-value]


class Settings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8341
    # WebSocket への送信 1 回の上限 [s]。超えたらその接続を閉じる。
    ws_send_timeout_s: float = 10.0
    listen_default: bool = True
    anim_mouth_default: bool = True
    # 車輪駆動 (drive / drive_source)。
    drive_source_default: DriveSourceSetting = "hub"
    drive_rate_hz: float = 10.0
    drive_hold_s: float = 1.0
    max_v: float = 0.15
    max_w: float = 2.0
    wheel_diameter_mm: float = 37.0
    track_width_mm: float = 77.5
    # odom 送信レート (表示用なので 10 Hz。stackchan_move の 50 は記録用)。
    odom_hz: int = 10
    # カメラ fps の自動調整。
    camera_fps: int = 4
    camera_fps_auto: bool = True
    camera_fps_min: int = 2
    camera_fps_max: int = 10
    # STT / TTS プラグインの選択 (組み込み名・entry point 名・``module.path:factory``)。
    stt_plugin: str = "whisper"
    tts_plugin: str = "voicevox"
    # Hub 共通の音声処理 (stackchan_move と同じ名前・既定値)。
    sample_rate: int = 16000
    channels: int = 1
    stt_ng_words_path: str = "stt_ng_words.txt"
    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 800
    vad_speech_pad_ms: int = 300
    vad_max_speech_s: int = 30
    speak_prebuffer_s: float = 2.0
    speak_slice_s: float = 0.5
    # TTS の probe 間隔 [s]。
    tts_probe_interval_s: float = 30.0
    # VLM バックエンド (Ollama)。
    vlm_backend: VlmBackendSetting = "ollama"
    vlm_url: str = "http://127.0.0.1:11434"
    vlm_model: str = "qwen3.5:9b"
    vlm_timeout_s: float = 60.0
    vlm_keep_alive: str = "10m"
    vlm_num_ctx: int = 8192
    vlm_temperature: float = 0.2
    vlm_num_predict: int = 400
    vlm_min_version: str = "0.17.1"
    # 可用性の確認間隔 [s] (変化したら ``state vlm:*``)。
    vlm_refresh_interval_s: float = 30.0
    # 質問応答。
    vlm_qa_queue_max: int = 3
    vlm_qa_max_question_chars: int = 500
    vlm_qa_frame_max_age_s: float = 2.0
    vlm_qa_frame_keep: int = 20
    vlm_qa_system_prompt: str = DEFAULT_VLM_QA_SYSTEM_PROMPT
    # 音声の入力・出力の既定 (Hub 全体で単一の状態)。
    vlm_voice_input_default: bool = False
    vlm_voice_output_default: bool = False


settings = Settings(
    host=os.getenv("HUB_HOST", "0.0.0.0"),
    port=int(os.getenv("HUB_PORT", "8341")),
    ws_send_timeout_s=float(os.getenv("WS_SEND_TIMEOUT_S", "10")),
    listen_default=_env_flag("HUB_LISTEN_DEFAULT"),
    anim_mouth_default=_env_flag("HUB_ANIM_MOUTH_DEFAULT"),
    drive_source_default=_env_drive_source("HUB_DRIVE_SOURCE_DEFAULT"),
    drive_rate_hz=float(os.getenv("HUB_DRIVE_RATE_HZ", "10")),
    drive_hold_s=float(os.getenv("HUB_DRIVE_HOLD_S", "1.0")),
    max_v=float(os.getenv("HUB_MAX_V", "0.15")),
    max_w=float(os.getenv("HUB_MAX_W", "2.0")),
    wheel_diameter_mm=float(os.getenv("HUB_WHEEL_DIAMETER_MM", "37.0")),
    track_width_mm=float(os.getenv("HUB_TRACK_WIDTH_MM", "77.5")),
    odom_hz=clamp_odom_hz(int(os.getenv("HUB_ODOM_HZ", "10"))),
    camera_fps=int(os.getenv("HUB_CAMERA_FPS", "4")),
    camera_fps_auto=_env_flag("HUB_CAMERA_FPS_AUTO"),
    camera_fps_min=int(os.getenv("HUB_CAMERA_FPS_MIN", "2")),
    camera_fps_max=int(os.getenv("HUB_CAMERA_FPS_MAX", "10")),
    stt_plugin=_env_stt_plugin(),
    tts_plugin=os.getenv("TTS_PLUGIN", "").strip() or "voicevox",
    sample_rate=int(os.getenv("SAMPLE_RATE", "16000")),
    channels=int(os.getenv("CHANNELS", "1")),
    stt_ng_words_path=os.getenv("STT_NG_WORDS_PATH", "stt_ng_words.txt"),
    vad_threshold=float(os.getenv("VAD_THRESHOLD", "0.5")),
    vad_min_silence_ms=int(os.getenv("VAD_MIN_SILENCE_MS", "800")),
    vad_speech_pad_ms=int(os.getenv("VAD_SPEECH_PAD_MS", "300")),
    vad_max_speech_s=int(os.getenv("VAD_MAX_SPEECH_S", "30")),
    speak_prebuffer_s=float(os.getenv("SPEAK_PREBUFFER_S", "2.0")),
    speak_slice_s=float(os.getenv("SPEAK_SLICE_S", "0.5")),
    vlm_backend=_env_vlm_backend("VLM_BACKEND"),
    vlm_url=os.getenv("VLM_URL", "http://127.0.0.1:11434"),
    vlm_model=os.getenv("VLM_MODEL", "qwen3.5:9b"),
    vlm_timeout_s=float(os.getenv("VLM_TIMEOUT_S", "60")),
    vlm_keep_alive=os.getenv("VLM_KEEP_ALIVE", "10m").strip() or "10m",
    vlm_num_ctx=int(os.getenv("VLM_NUM_CTX", "8192")),
    vlm_temperature=float(os.getenv("VLM_TEMPERATURE", "0.2")),
    vlm_num_predict=int(os.getenv("VLM_NUM_PREDICT", "400")),
    vlm_min_version=os.getenv("VLM_MIN_VERSION", "0.17.1"),
    vlm_qa_queue_max=int(os.getenv("VLM_QA_QUEUE_MAX", "3")),
    vlm_qa_max_question_chars=int(os.getenv("VLM_QA_MAX_QUESTION_CHARS", "500")),
    vlm_qa_frame_max_age_s=float(os.getenv("VLM_QA_FRAME_MAX_AGE_S", "2.0")),
    vlm_qa_frame_keep=int(os.getenv("VLM_QA_FRAME_KEEP", "20")),
    vlm_qa_system_prompt=os.getenv("VLM_QA_SYSTEM_PROMPT", "").strip() or DEFAULT_VLM_QA_SYSTEM_PROMPT,
    vlm_voice_input_default=_env_flag("VLM_VOICE_INPUT_DEFAULT", "off"),
    vlm_voice_output_default=_env_flag("VLM_VOICE_OUTPUT_DEFAULT", "off"),
)
