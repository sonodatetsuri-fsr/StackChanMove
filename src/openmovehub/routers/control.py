"""``control`` コマンドの共通処理。

WebSocket (app / device いずれのロールからでも) と REST ``POST /control`` の
双方から同じ実装を呼び、挙動が分岐しないようにする。コマンドの値域は
``models.messages.ControlCommand`` (``face | listen | anim | drive | drive_source | camera | odom_reset | vlm_voice``)。

``vlm_voice {input?, output?}`` と listen の一方向の連動 (音声入力 ON は listen ON を伴い、
listen OFF は音声入力 OFF を伴う)。
"""

import logging
from typing import Any

from openmovehub.models.messages import (
    AnimMessage,
    FaceMessage,
    ListenMessage,
    OdomResetMessage,
    StateMessage,
)
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState
from openmovehub.services.vlm_qa import VlmQaService

logger = logging.getLogger(__name__)


async def _set_listen(on: bool, *, manager: ConnectionManager, listen_state: ListenState) -> None:
    """listen フラグを切り替え、device に ``listen{on}``、app に ``state listening:*`` を送る。"""
    listen_state.on = on
    await manager.send_json_to_devices(ListenMessage(type="listen", on=on))
    await manager.send_json_to_apps(StateMessage(type="state", value=f"listening:{'on' if on else 'off'}"))
    if not on:
        listen_state.purge()
    logger.info("Listen flag set to %s", on)


async def handle_control(
    command: str,
    params: dict[str, Any],
    *,
    manager: ConnectionManager,
    listen_state: ListenState,
    anim_state: AnimState,
    drive: DriveController,
    camera_relay: CameraRelay | None = None,
    vlm_qa: VlmQaService | None = None,
) -> None:
    """``control`` コマンドを適用する。"""
    if command == "face":
        expr = params.get("expr", "neutral")
        await manager.send_json_to_devices(FaceMessage(type="face", expr=expr))

    elif command == "listen":
        on = bool(params.get("on", True))
        await _set_listen(on, manager=manager, listen_state=listen_state)
        if not on and vlm_qa is not None:
            # 聞こえていないのに音声入力が ON と表示される状態を作らない。
            await vlm_qa.set_voice_input(False)

    elif command == "anim":
        unknown_keys = [key for key in params if key != "mouth"]
        if unknown_keys:
            # 首サーボ廃止に伴い "gesture" などの旧キーは無視する。
            logger.debug("control anim ignoring unknown params: %s", unknown_keys)
        if "mouth" not in params:
            logger.warning("control anim received with no mouth param")
        else:
            anim_state.mouth = bool(params["mouth"])
            await manager.send_json_to_devices(AnimMessage(type="anim", mouth=anim_state.mouth))
            await manager.send_json_to_apps(
                StateMessage(type="state", value=f"anim:mouth:{'on' if anim_state.mouth else 'off'}")
            )
            logger.info("Anim flags updated: mouth=%s", anim_state.mouth)

    elif command == "drive":
        v = float(params.get("v", 0))
        w = float(params.get("w", 0))
        await drive.apply_app_command(v, w)

    elif command == "drive_source":
        # hub / joystick 以外 (auto を含む) は DriveController が拒否し、現在値の state を返す。
        await drive.set_source(str(params.get("source")))

    elif command == "camera":
        if camera_relay is None:
            logger.warning("control camera received but no camera relay is configured")
            return
        msg = camera_relay.apply_control(params)
        await manager.send_json_to_devices(msg)
        logger.info("Camera settings: on=%s fps=%d quality=%d", msg.on, msg.fps, msg.quality)

    elif command == "odom_reset":
        await manager.send_json_to_devices(OdomResetMessage(type="odom_reset"))
        logger.info("Odometry reset sent to devices")

    elif command == "vlm_voice":
        if vlm_qa is None:
            logger.warning("control vlm_voice received but VLM QA is not configured")
            return
        if "output" in params and params["output"] is not None:
            await vlm_qa.set_voice_output(bool(params["output"]))
        if "input" in params and params["input"] is not None:
            want = bool(params["input"])
            accepted = await vlm_qa.set_voice_input(want)
            if want and accepted and not listen_state.on:
                # 音声入力 ON は聞き取り ON を伴う。
                await _set_listen(True, manager=manager, listen_state=listen_state)

    else:
        logger.warning("Unknown control command: %r", command)
