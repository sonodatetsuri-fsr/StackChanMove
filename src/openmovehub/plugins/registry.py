"""STT / TTS プラグインの選択と追加。

``STT_PLUGIN`` / ``TTS_PLUGIN`` の値を次の順に解決する。

1. 組み込み名: STT は ``whisper`` / ``none``、TTS は ``voicevox`` / ``none``。
2. entry point: グループ ``openmovehub.stt`` / ``openmovehub.tts`` に登録された名前
   (第三者パッケージを ``uv pip install`` すれば使える)。
3. ``module.path:factory`` 形式の直接指定 (リポジトリ内に置いた自作モジュール向け)。

ファクトリは ``factory(settings)`` で呼ぶ。解決できない・ファクトリが例外を出した場合は
エラーをログに出して ``none`` にフォールバックし、表示名を ``none (fallback: <理由>)`` にする。
組み込みプラグインのモジュールは選ばれたときだけ import する (faster-whisper の import を避けるため)。
"""

import importlib
import importlib.metadata
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from openmovehub.plugins.none import NONE_NAME, create_stt, create_tts

logger = logging.getLogger(__name__)

Kind = Literal["stt", "tts"]

#: 組み込みプラグイン: 名前 → ``module:factory``。
BUILTIN: dict[str, dict[str, str]] = {
    "stt": {
        "whisper": "openmovehub.plugins.stt_whisper:create",
        NONE_NAME: "openmovehub.plugins.none:create_stt",
    },
    "tts": {
        "voicevox": "openmovehub.plugins.tts_voicevox:create",
        NONE_NAME: "openmovehub.plugins.none:create_tts",
    },
}

#: entry point のグループ名。
ENTRY_POINT_GROUPS: dict[str, str] = {"stt": "openmovehub.stt", "tts": "openmovehub.tts"}

_NONE_FACTORIES: dict[str, Callable[[Any], Any]] = {"stt": create_stt, "tts": create_tts}


@dataclass
class LoadedPlugin:
    """解決したプラグインと表示名 (``/status.audio.*.plugin``)。"""

    plugin: Any
    label: str
    fallback_reason: str | None = None


def _entry_points(kind: Kind) -> list[importlib.metadata.EntryPoint]:
    try:
        return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUPS[kind]))
    except Exception:  # pragma: no cover - 壊れたメタデータ
        logger.exception("Failed to read entry points for %s", ENTRY_POINT_GROUPS[kind])
        return []


def _load_object(spec: str) -> Any:
    """``module.path:attr`` を import して返す。"""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"not a 'module.path:factory' spec: {spec!r}")
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def resolve_factory(kind: Kind, name: str) -> Callable[[Any], Any]:
    """名前からファクトリを解決する。見つからなければ ``LookupError``。"""
    builtin = BUILTIN[kind].get(name)
    if builtin is not None:
        return _load_object(builtin)
    for ep in _entry_points(kind):
        if ep.name == name:
            return ep.load()
    if ":" in name:
        return _load_object(name)
    raise LookupError(f"unknown {kind} plugin: {name!r}")


def load_plugin(kind: Kind, name: str, settings: Any) -> LoadedPlugin:
    """プラグインを解決して生成する。失敗したら ``none`` にフォールバックする (Hub は止めない)。"""
    try:
        factory = resolve_factory(kind, name)
        plugin = factory(settings)
        if not getattr(plugin, "name", None):
            raise TypeError(f"{kind} plugin {name!r} has no 'name' attribute")
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        logger.error("%s plugin %r could not be loaded (%s); falling back to none", kind.upper(), name, reason)
        return LoadedPlugin(
            plugin=_NONE_FACTORIES[kind](settings),
            label=f"{NONE_NAME} (fallback: {reason})",
            fallback_reason=reason,
        )
    return LoadedPlugin(plugin=plugin, label=name)


def available_plugins(kind: Kind) -> list[str]:
    """組み込み + entry point の名前一覧 (重複は除く、順序は組み込みが先)。"""
    names = list(BUILTIN[kind])
    for ep in _entry_points(kind):
        if ep.name not in names:
            names.append(ep.name)
    return names
