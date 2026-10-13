"""設定 (env と ``.env``)。"""

import importlib

import pytest

import openmovehub.config as config_module
from openmovehub.config import Settings, _env_drive_source, _env_flag, clamp_odom_hz

_ENV_KEYS = (
    "HUB_HOST",
    "HUB_PORT",
    "HUB_DRIVE_SOURCE_DEFAULT",
    "HUB_DRIVE_RATE_HZ",
    "HUB_DRIVE_HOLD_S",
    "HUB_MAX_V",
    "HUB_MAX_W",
    "HUB_WHEEL_DIAMETER_MM",
    "HUB_TRACK_WIDTH_MM",
    "HUB_ODOM_HZ",
    "HUB_CAMERA_FPS",
    "HUB_CAMERA_FPS_AUTO",
    "HUB_CAMERA_FPS_MIN",
    "HUB_CAMERA_FPS_MAX",
    "HUB_LISTEN_DEFAULT",
    "HUB_ANIM_MOUTH_DEFAULT",
    "HUB_STT_ENABLED",
    "STT_PLUGIN",
    "TTS_PLUGIN",
    "SAMPLE_RATE",
    "SPEAK_PREBUFFER_S",
    "SPEAK_SLICE_S",
)


@pytest.fixture
def reload_config(monkeypatch):
    """env を設定してから ``openmovehub.config`` を読み直し、モジュール単位の ``settings`` を返す。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    def _reload(**env: str):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config_module).settings

    yield _reload
    monkeypatch.undo()
    importlib.reload(config_module)


def test_settings_defaults():
    s = Settings()
    assert s.host == "0.0.0.0"
    assert s.port == 8341
    assert s.listen_default is True
    assert s.anim_mouth_default is True


def test_settings_drive_defaults():
    s = Settings()
    assert s.drive_source_default == "hub"
    assert s.drive_rate_hz == 10.0
    assert s.drive_hold_s == 1.0
    assert s.max_v == 0.15
    assert s.max_w == 2.0
    assert s.wheel_diameter_mm == 37.0
    assert s.track_width_mm == 77.5


def test_settings_odom_and_camera_defaults():
    s = Settings()
    assert s.odom_hz == 10
    assert (s.camera_fps, s.camera_fps_auto, s.camera_fps_min, s.camera_fps_max) == (4, True, 2, 10)


def test_settings_override_and_mutable():
    s = Settings(host="127.0.0.1", port=9000)
    assert (s.host, s.port) == ("127.0.0.1", 9000)
    s.port = 9001
    assert s.port == 9001


def test_module_settings_defaults_without_env(reload_config):
    s = reload_config()
    assert s.host == "0.0.0.0"
    assert s.port == 8341
    assert s.odom_hz == 10
    assert s.drive_source_default == "hub"
    assert s.camera_fps_auto is True


def test_module_settings_from_env(reload_config):
    s = reload_config(
        HUB_HOST="192.168.1.1",
        HUB_PORT="9999",
        HUB_DRIVE_SOURCE_DEFAULT="joystick",
        HUB_MAX_V="0.1",
        HUB_ODOM_HZ="20",
        HUB_CAMERA_FPS="6",
        HUB_CAMERA_FPS_AUTO="off",
        HUB_CAMERA_FPS_MIN="3",
        HUB_CAMERA_FPS_MAX="8",
        HUB_LISTEN_DEFAULT="off",
        HUB_ANIM_MOUTH_DEFAULT="false",
    )
    assert s.host == "192.168.1.1"
    assert s.port == 9999
    assert s.drive_source_default == "joystick"
    assert s.max_v == 0.1
    assert s.odom_hz == 20
    assert (s.camera_fps, s.camera_fps_auto, s.camera_fps_min, s.camera_fps_max) == (6, False, 3, 8)
    assert s.listen_default is False
    assert s.anim_mouth_default is False


@pytest.mark.parametrize(("raw", "expected"), [("1", 5), ("5", 5), ("10", 10), ("50", 50), ("999", 50)])
def test_module_settings_odom_hz_is_clamped(reload_config, raw, expected):
    assert reload_config(HUB_ODOM_HZ=raw).odom_hz == expected


@pytest.mark.parametrize("raw", ["auto", "AUTO", "bogus", ""])
def test_module_settings_drive_source_default_rejects_auto(reload_config, raw):
    """``auto`` (と未知の値) は ``hub`` にする。"""
    assert reload_config(HUB_DRIVE_SOURCE_DEFAULT=raw).drive_source_default == "hub"


def test_env_flag(monkeypatch):
    monkeypatch.setenv("HUB_CAMERA_FPS_AUTO", "off")
    assert _env_flag("HUB_CAMERA_FPS_AUTO") is False
    monkeypatch.setenv("HUB_CAMERA_FPS_AUTO", "0")
    assert _env_flag("HUB_CAMERA_FPS_AUTO") is False
    monkeypatch.setenv("HUB_CAMERA_FPS_AUTO", "on")
    assert _env_flag("HUB_CAMERA_FPS_AUTO") is True


def test_env_drive_source(monkeypatch):
    monkeypatch.setenv("HUB_DRIVE_SOURCE_DEFAULT", "joystick")
    assert _env_drive_source("HUB_DRIVE_SOURCE_DEFAULT") == "joystick"
    monkeypatch.setenv("HUB_DRIVE_SOURCE_DEFAULT", " Hub ")
    assert _env_drive_source("HUB_DRIVE_SOURCE_DEFAULT") == "hub"
    monkeypatch.setenv("HUB_DRIVE_SOURCE_DEFAULT", "auto")
    assert _env_drive_source("HUB_DRIVE_SOURCE_DEFAULT") == "hub"


def test_settings_rejects_auto_drive_source():
    with pytest.raises(ValueError):
        Settings(drive_source_default="auto")


def test_clamp_odom_hz():
    assert clamp_odom_hz(0) == 5
    assert clamp_odom_hz(10) == 10
    assert clamp_odom_hz(100) == 50


# --- 音声 ----------------------------------------------------------------


def test_settings_audio_defaults():
    s = Settings()
    assert (s.stt_plugin, s.tts_plugin) == ("whisper", "voicevox")
    assert (s.sample_rate, s.channels) == (16000, 1)
    assert (s.speak_prebuffer_s, s.speak_slice_s) == (2.0, 0.5)
    assert (s.vad_threshold, s.vad_min_silence_ms, s.vad_speech_pad_ms, s.vad_max_speech_s) == (0.5, 800, 300, 30)
    assert s.stt_ng_words_path == "stt_ng_words.txt"


def test_module_settings_audio_plugins_from_env(reload_config):
    s = reload_config(STT_PLUGIN="none", TTS_PLUGIN="mypkg.tts:create")
    assert (s.stt_plugin, s.tts_plugin) == ("none", "mypkg.tts:create")
    assert config_module.DEPRECATION_WARNINGS == []


def test_module_settings_hub_stt_enabled_off_is_none_with_deprecation(reload_config):
    s = reload_config(HUB_STT_ENABLED="off")
    assert s.stt_plugin == "none"
    assert len(config_module.DEPRECATION_WARNINGS) == 1
    assert "STT_PLUGIN=none" in config_module.DEPRECATION_WARNINGS[0]


def test_module_settings_hub_stt_enabled_on_keeps_stt_plugin(reload_config):
    s = reload_config(HUB_STT_ENABLED="on", STT_PLUGIN="whisper")
    assert s.stt_plugin == "whisper"
    assert config_module.DEPRECATION_WARNINGS == []
