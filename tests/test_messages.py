import json

import pytest

from openmovehub.config import clamp_odom_hz
from openmovehub.models.messages import (
    CameraMessage,
    ControlMessage,
    DriveMessage,
    DriveSourceMessage,
    EventMessage,
    FaceMessage,
    HeardMessage,
    JoyMessage,
    ListenMessage,
    OdomMessage,
    OdomRateMessage,
    OdomResetMessage,
    SpeakEndMessage,
    SpeakMessage,
    SpeakStartMessage,
    StateMessage,
    parse_message,
)
from openmovehub.services.camera_relay import clamp_fps, clamp_quality


def _json(obj: dict) -> str:
    return json.dumps(obj)


class TestParseMessage:
    def test_listen_on(self):
        msg = parse_message(_json({"type": "listen", "on": True}))
        assert isinstance(msg, ListenMessage)
        assert msg.on is True

    def test_listen_off(self):
        msg = parse_message(_json({"type": "listen", "on": False}))
        assert isinstance(msg, ListenMessage)
        assert msg.on is False

    def test_speak_start(self):
        msg = parse_message(_json({"type": "speak_start"}))
        assert isinstance(msg, SpeakStartMessage)

    def test_speak_end(self):
        msg = parse_message(_json({"type": "speak_end"}))
        assert isinstance(msg, SpeakEndMessage)

    def test_face(self):
        msg = parse_message(_json({"type": "face", "expr": "happy"}))
        assert isinstance(msg, FaceMessage)
        assert msg.expr == "happy"

    def test_face_invalid_expr(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "face", "expr": "angry2"}))

    def test_legacy_pan_tilt_pose_rejected(self):
        """``pose`` は本プロジェクトのメッセージ型に無い (stackchan_move の姿勢は削った)。"""
        with pytest.raises(ValueError, match="Unknown message type"):
            parse_message(_json({"type": "pose", "pan": 0.0, "tilt": 45.0}))

    def test_control_volume_rejected(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "control", "command": "volume", "params": {"value": 5}}))

    def test_heard(self):
        msg = parse_message(_json({"type": "heard", "text": "こんにちは", "intent": "greet"}))
        assert isinstance(msg, HeardMessage)
        assert msg.text == "こんにちは"
        assert msg.intent == "greet"

    def test_wake_is_unknown(self):
        with pytest.raises(ValueError, match="Unknown message type"):
            parse_message(_json({"type": "wake"}))

    def test_state(self):
        msg = parse_message(_json({"type": "state", "value": "idle"}))
        assert isinstance(msg, StateMessage)
        assert msg.value == "idle"

    def test_speak_minimal(self):
        msg = parse_message(_json({"type": "speak", "text": "テスト"}))
        assert isinstance(msg, SpeakMessage)
        assert msg.text == "テスト"
        assert msg.emotion is None

    def test_speak_full(self):
        msg = parse_message(_json({"type": "speak", "text": "テスト", "emotion": "happy"}))
        assert isinstance(msg, SpeakMessage)
        assert msg.emotion == "happy"

    def test_speak_ignores_pose_field(self):
        msg = parse_message(_json({"type": "speak", "text": "テスト", "pose": {"pan": 10, "tilt": 60}}))
        assert isinstance(msg, SpeakMessage)
        assert not hasattr(msg, "pose")

    def test_control(self):
        msg = parse_message(_json({"type": "control", "command": "face", "params": {"expr": "sad"}}))
        assert isinstance(msg, ControlMessage)
        assert msg.command == "face"
        assert msg.params == {"expr": "sad"}

    def test_control_invalid_command(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "control", "command": "unknown", "params": {}}))

    def test_unknown_type(self):
        with pytest.raises(ValueError, match="Unknown message type"):
            parse_message(_json({"type": "nonexistent"}))

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            parse_message("not json")


class TestMessageSerialization:
    def test_listen_round_trip(self):
        original = {"type": "listen", "on": True}
        msg = parse_message(_json(original))
        assert msg.model_dump() == original

    def test_speak_round_trip(self):
        msg = parse_message(_json({"type": "speak", "text": "hello"}))
        dumped = msg.model_dump(exclude_none=True)
        assert dumped == {"type": "speak", "text": "hello"}


_ODOM_PAYLOAD = {
    "type": "odom",
    "ts": 123456,
    "seq": 7,
    "x": 0.25,
    "y": -0.1,
    "th": 0.5,
    "v": 0.15,
    "w": -0.2,
    "dl": 0.01,
    "dr": 0.02,
    "cmd": {"v": 0.15, "w": -0.2, "src": "hub"},
    "wheel": {"l_pos": 512, "r_pos": -512, "l_valid": True, "r_valid": False},
}

#: 拡張キー付きの odom。旧 payload (``_ODOM_PAYLOAD``) との差は ``wheel`` のみ。
_ODOM_PAYLOAD_V2 = dict(
    _ODOM_PAYLOAD,
    wheel={
        "l_pos": 792,
        "r_pos": 659,
        "l_valid": True,
        "r_valid": True,
        "l_state": "locked",
        "r_state": "crossing",
        "l_src": "meas",
        "r_src": "cmd",
        "stats": {
            "l": {"cross": 12, "lost": 0, "reject": 3, "extrap_rad": 4.2},
            "r": {"cross": 13, "lost": 1, "reject": 41, "extrap_rad": 9.8},
        },
    },
)


class TestDriveMessages:
    """drive / drive_source / odom_reset / camera (Hub -> device)."""

    def test_drive(self):
        msg = parse_message(_json({"type": "drive", "v": 0.1, "w": -0.5, "seq": 3, "src": "hub"}))
        assert isinstance(msg, DriveMessage)
        assert (msg.v, msg.w, msg.seq, msg.src) == (0.1, -0.5, 3, "hub")

    def test_drive_src_auto(self):
        msg = parse_message(_json({"type": "drive", "v": 0.0, "w": 0.0, "seq": 1, "src": "auto"}))
        assert isinstance(msg, DriveMessage)
        assert msg.src == "auto"

    def test_drive_src_joystick_rejected(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "drive", "v": 0.0, "w": 0.0, "seq": 1, "src": "joystick"}))

    def test_drive_source(self):
        for source in ("hub", "joystick"):
            msg = parse_message(_json({"type": "drive_source", "source": source}))
            assert isinstance(msg, DriveSourceMessage)
            assert msg.source == source

    def test_drive_source_auto_rejected(self):
        """駆動ソースから ``auto`` を除いた。"""
        with pytest.raises(ValueError):
            parse_message(_json({"type": "drive_source", "source": "auto"}))

    def test_drive_source_invalid(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "drive_source", "source": "manual"}))

    def test_wheel_pwm_is_unknown(self):
        """stackchan_move の ``wheel_pwm`` は Hub から送らないので型を持たない。"""
        with pytest.raises(ValueError, match="Unknown message type"):
            parse_message(_json({"type": "wheel_pwm", "l": 150, "r": -150, "seq": 9}))

    def test_odom_reset(self):
        msg = parse_message(_json({"type": "odom_reset"}))
        assert isinstance(msg, OdomResetMessage)

    def test_camera(self):
        msg = parse_message(_json({"type": "camera", "on": True, "fps": 5, "quality": 12}))
        assert isinstance(msg, CameraMessage)
        assert (msg.on, msg.fps, msg.quality) == (True, 5, 12)


class TestDeviceToHubMessages:
    """odom / joy / event (device -> Hub)."""

    def test_odom(self):
        msg = parse_message(_json(_ODOM_PAYLOAD))
        assert isinstance(msg, OdomMessage)
        assert msg.seq == 7
        assert msg.cmd.src == "hub"
        assert msg.wheel.l_pos == 512
        assert msg.wheel.r_valid is False

    def test_odom_round_trip(self):
        """拡張キーの無い旧ファーム payload は ``exclude_none`` でそのまま往復する。"""
        msg = parse_message(_json(_ODOM_PAYLOAD))
        assert json.loads(msg.model_dump_json(exclude_none=True)) == _ODOM_PAYLOAD

    def test_odom_legacy_wheel_extension_fields_are_none(self):
        """拡張キーはすべて任意 (旧ファーム互換)。"""
        msg = parse_message(_json(_ODOM_PAYLOAD))
        assert isinstance(msg, OdomMessage)
        w = msg.wheel
        assert (w.l_state, w.r_state, w.l_src, w.r_src, w.stats) == (None, None, None, None, None)

    def test_odom_wheel_extended(self):
        """拡張 payload (state / src / stats)。"""
        msg = parse_message(_json(_ODOM_PAYLOAD_V2))
        assert isinstance(msg, OdomMessage)
        w = msg.wheel
        assert (w.l_state, w.r_state) == ("locked", "crossing")
        assert (w.l_src, w.r_src) == ("meas", "cmd")
        assert w.stats is not None
        assert w.stats.l.model_dump() == {"cross": 12, "lost": 0, "reject": 3, "extrap_rad": 4.2}
        assert (w.stats.r.cross, w.stats.r.lost, w.stats.r.reject) == (13, 1, 41)
        assert w.stats.r.extrap_rad == pytest.approx(9.8)

    def test_odom_wheel_extended_round_trip(self):
        msg = parse_message(_json(_ODOM_PAYLOAD_V2))
        assert json.loads(msg.model_dump_json(exclude_none=True)) == _ODOM_PAYLOAD_V2

    def test_odom_wheel_stats_optional(self):
        """``stats`` は 10 回に 1 回だけ載る (無い odom も受理)。"""
        wheel = {k: v for k, v in _ODOM_PAYLOAD_V2["wheel"].items() if k != "stats"}
        msg = parse_message(_json(dict(_ODOM_PAYLOAD_V2, wheel=wheel)))
        assert isinstance(msg, OdomMessage)
        assert msg.wheel.stats is None
        assert msg.wheel.l_state == "locked"

    @pytest.mark.parametrize("key", ["l_state", "r_state"])
    def test_odom_wheel_invalid_state_rejected(self, key):
        wheel = dict(_ODOM_PAYLOAD_V2["wheel"], **{key: "confused"})
        with pytest.raises(ValueError):
            parse_message(_json(dict(_ODOM_PAYLOAD_V2, wheel=wheel)))

    @pytest.mark.parametrize("key", ["l_src", "r_src"])
    def test_odom_wheel_invalid_src_rejected(self, key):
        wheel = dict(_ODOM_PAYLOAD_V2["wheel"], **{key: "guess"})
        with pytest.raises(ValueError):
            parse_message(_json(dict(_ODOM_PAYLOAD_V2, wheel=wheel)))

    def test_odom_wheel_stats_requires_all_fields(self):
        stats = {
            "l": {"cross": 1, "lost": 0, "reject": 0},
            "r": {"cross": 1, "lost": 0, "reject": 0, "extrap_rad": 0.0},
        }
        wheel = dict(_ODOM_PAYLOAD_V2["wheel"], stats=stats)
        with pytest.raises(ValueError):
            parse_message(_json(dict(_ODOM_PAYLOAD_V2, wheel=wheel)))

    def test_odom_with_sim_truth_key_parses(self):
        """シミュレータの ``truth{x,y,th}`` など未知キーは無視して受理する。"""
        payload = dict(_ODOM_PAYLOAD_V2, truth={"x": 1.0, "y": -2.0, "th": 0.25})
        msg = parse_message(_json(payload))
        assert isinstance(msg, OdomMessage)
        assert msg.x == _ODOM_PAYLOAD_V2["x"]
        assert not hasattr(msg, "truth")
        assert json.loads(msg.model_dump_json(exclude_none=True)) == _ODOM_PAYLOAD_V2

    def test_odom_cmd_src_calib(self):
        """生 PWM 適用中の端末は ``cmd.src = calib`` を返す。"""
        payload = dict(_ODOM_PAYLOAD, cmd={"v": 0.0, "w": 0.0, "src": "calib"})
        msg = parse_message(_json(payload))
        assert isinstance(msg, OdomMessage)
        assert msg.cmd.src == "calib"

    def test_odom_cmd_src_none(self):
        payload = dict(_ODOM_PAYLOAD, cmd={"v": 0.0, "w": 0.0, "src": "none"})
        msg = parse_message(_json(payload))
        assert isinstance(msg, OdomMessage)
        assert msg.cmd.src == "none"

    def test_odom_missing_wheel_rejected(self):
        payload = {k: v for k, v in _ODOM_PAYLOAD.items() if k != "wheel"}
        with pytest.raises(ValueError):
            parse_message(_json(payload))

    def test_joy(self):
        msg = parse_message(_json({"type": "joy", "x": -1.0, "y": 0.5, "btn": True}))
        assert isinstance(msg, JoyMessage)
        assert (msg.x, msg.y, msg.btn) == (-1.0, 0.5, True)

    def test_event(self):
        msg = parse_message(_json({"type": "event", "name": "estop", "detail": "button"}))
        assert isinstance(msg, EventMessage)
        assert msg.name == "estop"
        assert msg.detail == "button"

    def test_event_detail_defaults_empty(self):
        msg = parse_message(_json({"type": "event", "name": "boot"}))
        assert isinstance(msg, EventMessage)
        assert msg.detail == ""

    def test_event_unknown_name_rejected(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "event", "name": "explode"}))


class TestControlCommands:
    """control.command Literal (face|listen|anim|drive|drive_source|camera|odom_reset)。"""

    @pytest.mark.parametrize(
        "command",
        ["face", "listen", "anim", "drive", "drive_source", "odom_reset", "camera"],
    )
    def test_accepted_commands(self, command):
        msg = parse_message(_json({"type": "control", "command": command, "params": {}}))
        assert isinstance(msg, ControlMessage)
        assert msg.command == command

    @pytest.mark.parametrize(
        "command",
        ["record", "explore", "odom_rate", "wheel_pwm", "train", "autonomy", "verify", "pose"],
    )
    def test_removed_commands_rejected(self, command):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "control", "command": command, "params": {}}))


class TestCameraMessages:
    """camera / control camera とクランプ規則。"""

    def test_camera_message(self):
        msg = parse_message(_json({"type": "camera", "on": True, "fps": 5, "quality": 60}))
        assert isinstance(msg, CameraMessage)
        assert (msg.on, msg.fps, msg.quality) == (True, 5, 60)

    def test_control_camera_message(self):
        msg = parse_message(_json({"type": "control", "command": "camera", "params": {"on": False, "fps": 2}}))
        assert isinstance(msg, ControlMessage)
        assert msg.command == "camera"
        assert msg.params == {"on": False, "fps": 2}

    @pytest.mark.parametrize(("given", "expected"), [(-1, 1), (0, 1), (1, 1), (5, 5), (10, 10), (11, 10), (999, 10)])
    def test_clamp_fps(self, given, expected):
        assert clamp_fps(given) == expected

    @pytest.mark.parametrize(("given", "expected"), [(0, 10), (9, 10), (10, 10), (60, 60), (90, 90), (91, 90)])
    def test_clamp_quality(self, given, expected):
        assert clamp_quality(given) == expected


class TestOdomRateMessages:
    """odom_rate (Hub -> device) と clamp_odom_hz。"""

    def test_odom_rate_message(self):
        msg = parse_message(_json({"type": "odom_rate", "hz": 50}))
        assert isinstance(msg, OdomRateMessage)
        assert (msg.type, msg.hz) == ("odom_rate", 50)

    def test_odom_rate_round_trip(self):
        msg = parse_message(_json({"type": "odom_rate", "hz": 25}))
        assert json.loads(msg.model_dump_json()) == {"type": "odom_rate", "hz": 25}

    def test_odom_rate_requires_hz(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "odom_rate"}))

    def test_odom_rate_rejects_non_numeric_hz(self):
        with pytest.raises(ValueError):
            parse_message(_json({"type": "odom_rate", "hz": "fast"}))

    @pytest.mark.parametrize(("given", "expected"), [(0, 5), (4, 5), (5, 5), (25, 25), (50, 50), (99, 50)])
    def test_clamp_odom_hz(self, given, expected):
        assert clamp_odom_hz(given) == expected
