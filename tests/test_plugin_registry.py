"""プラグインの解決 (組み込み → entry point → module:factory) と none フォールバック。"""

import importlib.metadata
from unittest.mock import patch

from openmovehub.config import Settings
from openmovehub.plugins import registry
from openmovehub.plugins.base import SttPlugin, TtsPlugin
from openmovehub.plugins.none import NoneStt, NoneTts
from tests.plugin_fixtures.dummy_plugins import DummyStt, DummyTts

DUMMY = "tests.plugin_fixtures.dummy_plugins"


def _eps(group: str, **names: str) -> list[importlib.metadata.EntryPoint]:
    return [importlib.metadata.EntryPoint(name=n, value=v, group=group) for n, v in names.items()]


def _patch_entry_points(stt: dict[str, str] | None = None, tts: dict[str, str] | None = None):
    def fake(*, group: str):
        if group == "openmovehub.stt":
            return _eps(group, **(stt or {}))
        if group == "openmovehub.tts":
            return _eps(group, **(tts or {}))
        return []

    return patch.object(registry.importlib.metadata, "entry_points", side_effect=fake)


def test_builtin_none_plugins():
    stt = registry.load_plugin("stt", "none", Settings())
    tts = registry.load_plugin("tts", "none", Settings())
    assert isinstance(stt.plugin, NoneStt) and stt.label == "none"
    assert isinstance(tts.plugin, NoneTts) and tts.label == "none"
    assert isinstance(stt.plugin, SttPlugin)
    assert isinstance(tts.plugin, TtsPlugin)


def test_builtin_voicevox_resolves():
    loaded = registry.load_plugin("tts", "voicevox", Settings())
    assert loaded.plugin.name == "voicevox"
    assert loaded.label == "voicevox"
    assert loaded.fallback_reason is None


def test_builtin_whisper_factory_resolves_without_loading_a_model():
    with patch("ctranslate2.get_cuda_device_count", return_value=0):
        loaded = registry.load_plugin("stt", "whisper", Settings())
    assert loaded.plugin.name == "whisper"
    assert loaded.plugin._model is None
    assert loaded.plugin.status()["device"] == "cpu"


def test_entry_point_plugin():
    with _patch_entry_points(stt={"mystt": f"{DUMMY}:create_stt"}, tts={"mytts": f"{DUMMY}:create_tts"}):
        stt = registry.load_plugin("stt", "mystt", Settings())
        tts = registry.load_plugin("tts", "mytts", Settings())
    assert isinstance(stt.plugin, DummyStt) and stt.label == "mystt"
    assert isinstance(tts.plugin, DummyTts) and tts.label == "mytts"


def test_factory_receives_settings():
    settings = Settings(port=9999)
    loaded = registry.load_plugin("stt", f"{DUMMY}:create_stt", settings)
    assert loaded.plugin.settings is settings


def test_module_factory_plugin():
    loaded = registry.load_plugin("tts", f"{DUMMY}:create_tts", Settings())
    assert isinstance(loaded.plugin, DummyTts)
    assert loaded.label == f"{DUMMY}:create_tts"


def test_builtin_name_wins_over_entry_point():
    with _patch_entry_points(tts={"none": f"{DUMMY}:create_tts"}):
        loaded = registry.load_plugin("tts", "none", Settings())
    assert isinstance(loaded.plugin, NoneTts)


def test_unknown_name_falls_back_to_none(caplog):
    with _patch_entry_points():
        loaded = registry.load_plugin("stt", "nosuch", Settings())
    assert isinstance(loaded.plugin, NoneStt)
    assert loaded.label.startswith("none (fallback: ")
    assert "nosuch" in loaded.label
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_missing_module_falls_back_to_none():
    loaded = registry.load_plugin("tts", "no_such_module_xyz:create", Settings())
    assert isinstance(loaded.plugin, NoneTts)
    assert loaded.label.startswith("none (fallback: ModuleNotFoundError")


def test_factory_exception_falls_back_to_none():
    loaded = registry.load_plugin("stt", f"{DUMMY}:broken_factory", Settings())
    assert isinstance(loaded.plugin, NoneStt)
    assert loaded.label == "none (fallback: RuntimeError: factory exploded)"


def test_entry_point_factory_exception_falls_back_to_none():
    with _patch_entry_points(stt={"bad": f"{DUMMY}:broken_factory"}):
        loaded = registry.load_plugin("stt", "bad", Settings())
    assert isinstance(loaded.plugin, NoneStt)
    assert "factory exploded" in loaded.label


def test_available_plugins_lists_builtins_and_entry_points():
    with _patch_entry_points(stt={"mystt": f"{DUMMY}:create_stt", "none": f"{DUMMY}:create_stt"}):
        assert registry.available_plugins("stt") == ["whisper", "none", "mystt"]
        assert registry.available_plugins("tts") == ["voicevox", "none"]
