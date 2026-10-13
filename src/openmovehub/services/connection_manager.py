"""WebSocket 接続の一覧と送信。

送信先ごとに並列に送り、上限時間 (``send_timeout_s``) 内に送れない・例外を出した接続は
一覧から外して閉じる。device / media は同じ送信元 IP の古い接続を新しい接続で置き換える。
"""

import asyncio
import logging

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

#: 失敗・置き換えた接続を閉じるときの上限 [s]。
CLOSE_TIMEOUT_S = 1.0
#: 送信に失敗した接続を閉じるときの close コード (internal error)。
CLOSE_CODE_SEND_FAILED = 1011
#: 置き換えた古い接続を閉じるときの close コード (service restart)。
CLOSE_CODE_REPLACED = 1012
#: 同じ送信元 IP の古い接続を置き換える役割 (app は置き換えない)。
REPLACE_STALE_ROLES = ("device", "media")


def _client_host(ws: WebSocket) -> str | None:
    """送信元 IP。``ws.client`` が無い (テストの偽物など) ときは ``None``。"""
    client = getattr(ws, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return host if isinstance(host, str) and host else None


async def _close_quietly(ws: WebSocket, code: int) -> None:
    """``ws.close(code)`` を上限 ``CLOSE_TIMEOUT_S`` で試す。失敗は無視する。"""
    try:
        await asyncio.wait_for(ws.close(code=code), timeout=CLOSE_TIMEOUT_S)
    except Exception:
        logger.debug("Closing WebSocket failed (ignored)", exc_info=True)


class ConnectionManager:
    def __init__(self, send_timeout_s: float = 10.0) -> None:
        self._devices: list[WebSocket] = []
        self._apps: list[WebSocket] = []
        self._media: list[WebSocket] = []
        self._lock = asyncio.Lock()
        # WebSocket への送信 1 回の上限 [s] (``WS_SEND_TIMEOUT_S``)。
        self._send_timeout_s = send_timeout_s

    async def connect(self, ws: WebSocket, role: str) -> None:
        await ws.accept()
        stale: list[WebSocket] = []
        host = _client_host(ws) if role in REPLACE_STALE_ROLES else None
        async with self._lock:
            if host is not None:
                # 同じ役割・同じ送信元 IP の古い接続を外す (端末のリセット後に残った接続)。
                current = self._devices if role == "device" else self._media
                stale = [w for w in current if w is not ws and _client_host(w) == host]
                if stale:
                    kept = [w for w in current if w not in stale]
                    if role == "device":
                        self._devices = kept
                    else:
                        self._media = kept
            if role == "device":
                self._devices.append(ws)
            elif role == "app":
                self._apps.append(ws)
            elif role == "media":
                self._media.append(ws)
        for old in stale:
            logger.info("Replaced stale %s connection from %s", role, host)
            await _close_quietly(old, CLOSE_CODE_REPLACED)
        logger.info(
            "Connected: role=%s, total_devices=%d, total_apps=%d, total_media=%d",
            role,
            len(self._devices),
            len(self._apps),
            len(self._media),
        )
        if role == "device":
            # 制御コネクション (device) の接続を明示する。
            logger.info("Device connected (%d)", len(self._devices))
        elif role == "media":
            # メディア (マイク PCM / カメラ JPEG) 専用コネクション。
            logger.info("media connected (%d)", len(self._media))

    async def disconnect(self, ws: WebSocket, role: str) -> None:
        async with self._lock:
            if role == "device":
                self._devices = [w for w in self._devices if w is not ws]
            elif role == "app":
                self._apps = [w for w in self._apps if w is not ws]
            elif role == "media":
                self._media = [w for w in self._media if w is not ws]
        logger.info("Disconnected: role=%s", role)
        if role == "device":
            logger.info("Device disconnected (%d)", len(self._devices))
        elif role == "media":
            logger.info("media disconnected (%d)", len(self._media))

    async def _send_one(self, ws: WebSocket, data: str | bytes, role: str) -> bool:
        """1 本の WebSocket に上限 ``send_timeout_s`` で送る。失敗・タイムアウトなら ``False``。"""
        try:
            if isinstance(data, bytes):
                await asyncio.wait_for(ws.send_bytes(data), timeout=self._send_timeout_s)
            else:
                await asyncio.wait_for(ws.send_text(data), timeout=self._send_timeout_s)
        except TimeoutError:
            logger.warning("Failed to send to WebSocket; removing connection (role=%s, timeout)", role)
            return False
        except Exception as e:
            logger.warning("Failed to send to WebSocket; removing connection (role=%s, %s)", role, type(e).__name__)
            logger.debug("Send failure detail (role=%s)", role, exc_info=True)
            return False
        return True

    async def _send_to_list(self, targets: list[WebSocket], data: str | bytes, role: str) -> list[WebSocket]:
        """送信先ごとに並列に送り、失敗した WebSocket の一覧を返す。"""
        if not targets:
            return []
        results = await asyncio.gather(*(self._send_one(ws, data, role) for ws in targets))
        return [ws for ws, ok in zip(targets, results, strict=True) if not ok]

    async def _remove_and_close(self, failed: list[WebSocket], role: str) -> None:
        """失敗した WebSocket を一覧から外し、``close(code=1011)`` を試す。"""
        async with self._lock:
            if role == "device":
                self._devices = [w for w in self._devices if w not in failed]
            elif role == "app":
                self._apps = [w for w in self._apps if w not in failed]
            elif role == "media":
                self._media = [w for w in self._media if w not in failed]
        await asyncio.gather(*(_close_quietly(ws, CLOSE_CODE_SEND_FAILED) for ws in failed))

    async def send_to_devices(self, data: str | bytes) -> None:
        async with self._lock:
            targets = list(self._devices)
        failed = await self._send_to_list(targets, data, "device")
        if failed:
            await self._remove_and_close(failed, "device")

    async def send_to_apps(self, data: str | bytes) -> None:
        async with self._lock:
            targets = list(self._apps)
        failed = await self._send_to_list(targets, data, "app")
        if failed:
            await self._remove_and_close(failed, "app")

    async def send_json_to_devices(self, msg: object) -> None:
        await self.send_to_devices(msg.model_dump_json())  # type: ignore[attr-defined]

    async def send_json_to_apps(self, msg: object) -> None:
        await self.send_to_apps(msg.model_dump_json())  # type: ignore[attr-defined]

    @property
    def devices(self) -> list[WebSocket]:
        return list(self._devices)

    @property
    def apps(self) -> list[WebSocket]:
        return list(self._apps)

    @property
    def media(self) -> list[WebSocket]:
        return list(self._media)

    @property
    def media_count(self) -> int:
        return len(self._media)
