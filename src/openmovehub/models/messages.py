import json
from typing import Any, Literal

from pydantic import BaseModel

#: 駆動ソースは hub / joystick の 2 値 (stackchan_move の ``auto`` は削った)。
DriveSource = Literal["hub", "joystick"]

#: ``control.command`` の値 (``vlm_voice`` を含む)。
ControlCommand = Literal["face", "listen", "anim", "drive", "drive_source", "camera", "odom_reset", "vlm_voice"]

#: VLM への質問の入力元。
VlmSource = Literal["text", "voice"]


class ListenMessage(BaseModel):
    type: Literal["listen"]
    on: bool


class SpeakStartMessage(BaseModel):
    type: Literal["speak_start"]


class SpeakEndMessage(BaseModel):
    type: Literal["speak_end"]


FaceExpr = Literal["neutral", "happy", "sad", "angry", "doubt", "sleepy"]


class FaceMessage(BaseModel):
    type: Literal["face"]
    expr: FaceExpr


class AnimMessage(BaseModel):
    type: Literal["anim"]
    mouth: bool


class DriveMessage(BaseModel):
    """Hub → device: 目標速度。v [m/s] 前進正、w [rad/s] 左旋回正。

    ``src`` は ``"hub"`` だけを送る (型はファーム互換のため ``"hub" | "auto"`` のまま)。
    """

    type: Literal["drive"]
    v: float
    w: float
    seq: int
    src: Literal["hub", "auto"]


class DriveSourceMessage(BaseModel):
    """Hub → device / 駆動ソースのスナップショット。"""

    type: Literal["drive_source"]
    source: DriveSource


class OdomResetMessage(BaseModel):
    """Hub → device: オドメトリ積算 (x, y, th) を 0 にする。"""

    type: Literal["odom_reset"]


class CameraMessage(BaseModel):
    """Hub → device: カメラ配信設定。

    ``control camera`` を受けた Hub が現在値で補った全量スナップショットとして送る。
    値域 (fps 1..10 / quality 10..90) のクランプは ``CameraRelay`` が行う。
    """

    type: Literal["camera"]
    on: bool
    fps: int
    quality: int


class OdomRateMessage(BaseModel):
    """Hub → device: odom 送信レート [Hz]。

    Hub は ``HUB_ODOM_HZ`` (既定 10、5..50 にクランプ) を device 接続時の greeting で送る。
    端末は NVS に保存しない。
    """

    type: Literal["odom_rate"]
    hz: int


class OdomCmd(BaseModel):
    """端末が実際に適用中の指令。"""

    v: float
    w: float
    # ``calib`` は生 PWM (``wheel_pwm``) 適用中の表示専用ソース。
    src: Literal["hub", "joystick", "auto", "none", "calib"]


WheelState = Literal["locked", "crossing", "lost"]
WheelSrc = Literal["meas", "cmd", "none"]


class OdomWheelSideStats(BaseModel):
    """片輪のトラッカ統計。``odom_reset`` ではクリアされない累積値。"""

    cross: int  # 不感帯の横断回数
    lost: int  # 再ロックできず lost に落ちた回数
    reject: int  # 物理上限で棄却した差分数
    extrap_rad: float  # 外挿で積算した角の累積絶対値 [rad]


class OdomWheelStats(BaseModel):
    """左右の :class:`OdomWheelSideStats` (stackchan_move と同じ、送信 10 回に 1 回だけ載る)。"""

    l: OdomWheelSideStats  # noqa: E741 — ワイヤ形式のキー名
    r: OdomWheelSideStats


class OdomWheel(BaseModel):
    """車輪サーボの生の位置と有効性 + トラッカの状態。

    stackchan_move の拡張キー (``l_state`` / ``r_state`` / ``l_src`` / ``r_src`` / ``stats``) は
    **すべて任意**で、旧ファームの ``odom`` (拡張キー無し) もそのまま受理する。
    ``l_valid`` / ``r_valid`` は 拡張キー導入以降 ``state == "locked"`` と同義。
    """

    l_pos: int
    r_pos: int
    l_valid: bool
    r_valid: bool
    # トラッカ状態と Δθ の出所 (省略時 None)。
    l_state: WheelState | None = None
    r_state: WheelState | None = None
    l_src: WheelSrc | None = None
    r_src: WheelSrc | None = None
    stats: OdomWheelStats | None = None


class OdomMessage(BaseModel):
    """device → Hub: オドメトリ。レートは ``odom_rate`` で指定 (既定 10 Hz)。

    ``ts`` は姿勢をサンプルした端末時刻 ``millis()``。
    """

    type: Literal["odom"]
    ts: int
    seq: int
    x: float
    y: float
    th: float
    v: float
    w: float
    dl: float
    dr: float
    cmd: OdomCmd
    wheel: OdomWheel


class JoyMessage(BaseModel):
    """device → Hub: リモコン (ESP-NOW) の生値。Hub は app へ中継するだけ。"""

    type: Literal["joy"]
    x: float
    y: float
    btn: bool


class EventMessage(BaseModel):
    """device → Hub: 端末イベント。"""

    type: Literal["event"]
    name: Literal["estop", "boot", "drive_timeout", "joy_link"]
    detail: str = ""


class HeardMessage(BaseModel):
    type: Literal["heard"]
    text: str
    intent: str


class StateMessage(BaseModel):
    type: Literal["state"]
    value: str


class SpeakMessage(BaseModel):
    type: Literal["speak"]
    text: str
    emotion: str | None = None


class ControlMessage(BaseModel):
    type: Literal["control"]
    command: ControlCommand
    params: dict[str, Any]


class VlmAskMessage(BaseModel):
    """app → Hub: VLM への質問。"""

    type: Literal["vlm_ask"]
    question: str


class VlmQueryMessage(BaseModel):
    """Hub → app: 質問の処理を始めた。音声で聞いた質問も全 app に送る。"""

    type: Literal["vlm_query"]
    id: int
    question: str
    source: VlmSource
    ts: float


class VlmAnswerMessage(BaseModel):
    """Hub → app: 回答。``error`` は ``no_frame | unavailable | timeout | busy | invalid``。"""

    type: Literal["vlm_answer"]
    id: int
    question: str
    answer: str
    source: VlmSource
    ms: float | None
    frame_seq: int | None
    spoken: bool
    error: str | None


HubToDeviceMessage = (
    ListenMessage
    | SpeakStartMessage
    | SpeakEndMessage
    | FaceMessage
    | AnimMessage
    | DriveMessage
    | DriveSourceMessage
    | OdomResetMessage
    | CameraMessage
    | OdomRateMessage
)
DeviceToHubMessage = OdomMessage | JoyMessage | EventMessage
HubToAppMessage = HeardMessage | StateMessage | VlmQueryMessage | VlmAnswerMessage
ClientToHubMessage = SpeakMessage | ControlMessage | VlmAskMessage

BaseMessage = HubToDeviceMessage | DeviceToHubMessage | HubToAppMessage | ClientToHubMessage

_TYPE_TO_MODEL: dict[str, type[BaseMessage]] = {
    "listen": ListenMessage,
    "speak_start": SpeakStartMessage,
    "speak_end": SpeakEndMessage,
    "face": FaceMessage,
    "anim": AnimMessage,
    "drive": DriveMessage,
    "drive_source": DriveSourceMessage,
    "odom_reset": OdomResetMessage,
    "camera": CameraMessage,
    "odom_rate": OdomRateMessage,
    "odom": OdomMessage,
    "joy": JoyMessage,
    "event": EventMessage,
    "heard": HeardMessage,
    "state": StateMessage,
    "speak": SpeakMessage,
    "control": ControlMessage,
    "vlm_ask": VlmAskMessage,
    "vlm_query": VlmQueryMessage,
    "vlm_answer": VlmAnswerMessage,
}


def parse_message(data: str) -> BaseMessage:
    raw = json.loads(data)
    msg_type = raw.get("type")
    model = _TYPE_TO_MODEL.get(msg_type)
    if model is None:
        raise ValueError(f"Unknown message type: {msg_type!r}")
    return model.model_validate(raw)
