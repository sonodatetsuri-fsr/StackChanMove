"""Web クライアント (web/index.html) の静的検査。"""

import pathlib
import re
import typing

import pytest

from openmovehub.models.messages import ControlCommand

WEB_INDEX = pathlib.Path(__file__).resolve().parent.parent / "web" / "index.html"

REQUIRED_IDS = [
    "cam",
    "conn-badge",
    "pad-fl",
    "pad-f",
    "pad-fr",
    "pad-l",
    "pad-stop",
    "pad-r",
    "pad-bl",
    "pad-b",
    "pad-br",
    "src-hub",
    "src-joy",
    "listen-toggle",
    "heard-list",
    "heard-clear",
    "speak-text",
    "speak-btn",
    "mouth-toggle",
    # VLM パネル。
    "panel-vlm",
    "vlm-state",
    "vlm-model",
    "vlm-voice-in-toggle",
    "vlm-voice-out-toggle",
    "vlm-question",
    "vlm-ask-btn",
    "vlm-voice-hint",
    "vlm-log",
    "vlm-frame-view",
    "vlm-frame-img",
]

COMMAND_RE = re.compile(r"""command\s*:\s*["']([A-Za-z_]+)["']""")


@pytest.fixture(scope="module")
def html() -> str:
    return WEB_INDEX.read_text(encoding="utf-8")


def test_index_exists():
    assert WEB_INDEX.is_file()


@pytest.mark.parametrize("element_id", REQUIRED_IDS)
def test_required_ids_exist(html: str, element_id: str):
    assert re.search(rf"""\bid=["']{re.escape(element_id)}["']""", html), element_id


def test_ids_are_unique(html: str):
    ids = re.findall(r"""\bid=["']([^"']+)["']""", html)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, dupes


def test_no_external_resources(html: str):
    assert not re.search(r"""<script[^>]*\bsrc\s*=\s*["']?\s*(https?:)?//""", html, re.I)
    assert not re.search(r"""<link[^>]*\bhref\s*=\s*["']?\s*(https?:)?//""", html, re.I)
    assert not re.search(r"""@import\s+(url\()?\s*["']?\s*(https?:)?//""", html, re.I)
    assert not re.search(r"""url\(\s*["']?\s*(https?:)?//""", html, re.I)
    assert "http://" not in html
    assert "https://" not in html


def test_no_internal_document_numbers(html: str):
    assert "ADR" not in html


def test_control_commands_are_known(html: str):
    known = set(typing.get_args(ControlCommand))
    found = COMMAND_RE.findall(html)
    assert len(found) >= 5, found
    unknown = sorted(set(found) - known)
    assert not unknown, unknown


def test_core_control_commands_are_used(html: str):
    found = set(COMMAND_RE.findall(html))
    assert {"drive", "drive_source", "camera", "listen", "anim"} <= found


def test_ws_endpoint_and_binary_type(html: str):
    assert "/ws/app" in html
    assert 'binaryType = "arraybuffer"' in html


def test_drive_pad_has_touch_action_none(html: str):
    assert re.search(r"#drive-pad\s*\{[^}]*touch-action:\s*none", html)


# --- VLM パネル -------------------------------------------------------


def test_vlm_voice_is_a_control_command():
    assert "vlm_voice" in typing.get_args(ControlCommand)


def test_vlm_voice_control_is_used(html: str):
    assert "vlm_voice" in set(COMMAND_RE.findall(html))
    assert re.search(r"""command:\s*"vlm_voice",\s*params:\s*\{\s*input:""", html)
    assert re.search(r"""command:\s*"vlm_voice",\s*params:\s*\{\s*output:""", html)


def test_vlm_ask_is_sent(html: str):
    assert re.search(r"""type:\s*["']vlm_ask["']\s*,\s*question""", html)


def test_vlm_messages_are_handled(html: str):
    assert 'case "vlm_query"' in html
    assert 'case "vlm_answer"' in html
    for prefix in ("vlm:", "vlm_voice_in:", "vlm_voice_out:"):
        assert f'value.startsWith("{prefix}")' in html


def test_vlm_panel_is_below_the_camera(html: str):
    # PC では左列のカメラの下、スマートフォンではカメラ → VLM → 駆動の順。
    cam = html.index('id="panel-camera"')
    vlm = html.index('id="panel-vlm"')
    drive = html.index('id="panel-drive"')
    assert cam < vlm < drive


def test_vlm_labels(html: str):
    for text in (
        "音声で質問",
        "音声で回答",
        "質問",
        "ロボットに話しかけると質問になります",
        "考え中…",
        "読み上げ済み",
        "使用可能",
        "接続できません",
        "無効",
        "カメラの画像が届いていません",
        "VLM へ",
        "/vlm/frame/",
    ):
        assert text in html, text
