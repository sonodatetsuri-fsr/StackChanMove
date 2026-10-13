import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openmovehub.services.connection_manager import ConnectionManager


def make_mock_ws():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_connect_device():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")
    ws.accept.assert_awaited_once()
    assert ws in manager.devices
    assert ws not in manager.apps


@pytest.mark.asyncio
async def test_connect_app():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "app")
    ws.accept.assert_awaited_once()
    assert ws in manager.apps
    assert ws not in manager.devices


@pytest.mark.asyncio
async def test_disconnect_device():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")
    await manager.disconnect(ws, "device")
    assert ws not in manager.devices


@pytest.mark.asyncio
async def test_disconnect_app():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "app")
    await manager.disconnect(ws, "app")
    assert ws not in manager.apps


@pytest.mark.asyncio
async def test_send_to_devices_text():
    manager = ConnectionManager()
    ws1 = make_mock_ws()
    ws2 = make_mock_ws()
    await manager.connect(ws1, "device")
    await manager.connect(ws2, "device")
    await manager.send_to_devices("hello")
    ws1.send_text.assert_awaited_once_with("hello")
    ws2.send_text.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_send_to_devices_bytes():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")
    await manager.send_to_devices(b"\x00\x01")
    ws.send_bytes.assert_awaited_once_with(b"\x00\x01")


@pytest.mark.asyncio
async def test_send_to_apps_text():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "app")
    await manager.send_to_apps("msg")
    ws.send_text.assert_awaited_once_with("msg")


@pytest.mark.asyncio
async def test_send_to_apps_bytes():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "app")
    await manager.send_to_apps(b"\xff")
    ws.send_bytes.assert_awaited_once_with(b"\xff")


@pytest.mark.asyncio
async def test_failed_connection_removed_from_devices():
    manager = ConnectionManager()
    ws = make_mock_ws()
    ws.send_text.side_effect = Exception("connection closed")
    await manager.connect(ws, "device")
    await manager.send_to_devices("hello")
    assert ws not in manager.devices


@pytest.mark.asyncio
async def test_failed_connection_removed_from_apps():
    manager = ConnectionManager()
    ws = make_mock_ws()
    ws.send_text.side_effect = Exception("connection closed")
    await manager.connect(ws, "app")
    await manager.send_to_apps("hello")
    assert ws not in manager.apps


@pytest.mark.asyncio
async def test_send_json_to_devices():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    from openmovehub.models.messages import FaceMessage

    msg = FaceMessage(type="face", expr="happy")
    await manager.send_json_to_devices(msg)
    ws.send_text.assert_awaited_once_with(msg.model_dump_json())


@pytest.mark.asyncio
async def test_send_json_to_apps():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "app")

    from openmovehub.models.messages import HeardMessage

    msg = HeardMessage(type="heard", text="こんにちは", intent="")
    await manager.send_json_to_apps(msg)
    ws.send_text.assert_awaited_once_with(msg.model_dump_json())


@pytest.mark.asyncio
async def test_devices_property_returns_copy():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")
    devices = manager.devices
    devices.clear()
    assert ws in manager.devices


@pytest.mark.asyncio
async def test_apps_property_returns_copy():
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "app")
    apps = manager.apps
    apps.clear()
    assert ws in manager.apps


@pytest.mark.asyncio
async def test_device_connect_disconnect_logs(caplog):
    """device 接続で ``Device connected`` がログに出る。"""
    manager = ConnectionManager()
    ws = make_mock_ws()
    with caplog.at_level(logging.INFO):
        await manager.connect(ws, "device")
        await manager.disconnect(ws, "device")
    assert "Device connected (1)" in caplog.text
    assert "Device disconnected (0)" in caplog.text


# --- 送信の並列化と失敗した接続の close ---------------------------------------------------------------------


def make_ws_from(host: str):
    ws = make_mock_ws()
    ws.client = SimpleNamespace(host=host, port=12345)
    ws.close = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_send_exception_removes_and_closes(caplog):
    """§1: 送信で例外を出す WebSocket は一覧から外され、close(code=1011) される。"""
    manager = ConnectionManager()
    bad = make_mock_ws()
    bad.send_text.side_effect = RuntimeError("connection closed")
    good = make_mock_ws()
    await manager.connect(bad, "device")
    await manager.connect(good, "device")
    with caplog.at_level(logging.WARNING):
        await manager.send_to_devices("hello")
    assert manager.devices == [good]
    bad.close.assert_awaited_once_with(code=1011)
    good.send_text.assert_awaited_once_with("hello")
    good.close.assert_not_awaited()
    assert "Failed to send to WebSocket; removing connection (role=device, RuntimeError)" in caplog.text
    # WARNING は 1 行 (トレースバックは DEBUG)。
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None


@pytest.mark.asyncio
async def test_send_exception_close_failure_is_ignored():
    """§1: close() が失敗しても無視して外す。"""
    manager = ConnectionManager()
    bad = make_mock_ws()
    bad.send_bytes.side_effect = RuntimeError("boom")
    bad.close = AsyncMock(side_effect=RuntimeError("already closed"))
    await manager.connect(bad, "app")
    await manager.send_to_apps(b"\x00")
    assert bad not in manager.apps
    bad.close.assert_awaited_once_with(code=1011)


@pytest.mark.asyncio
async def test_stuck_send_times_out_and_others_receive_immediately(caplog):
    """§1: 終わらない送信は上限時間で外されて閉じられ、他の送信先にはすぐ届く (並列)。"""
    manager = ConnectionManager(send_timeout_s=0.2)
    stuck = make_mock_ws()

    async def never_finishes(data):
        await asyncio.sleep(3600)

    stuck.send_bytes = AsyncMock(side_effect=never_finishes)
    good = make_mock_ws()
    good_received = asyncio.Event()
    good.send_bytes = AsyncMock(side_effect=lambda data: good_received.set())
    await manager.connect(stuck, "device")  # 先に並んでいる詰まった接続
    await manager.connect(good, "device")

    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(manager.send_to_devices(b"\x01\x02"))
        await asyncio.wait_for(good_received.wait(), timeout=0.1)
        assert loop.time() - started < 0.1
        assert not task.done()
        await asyncio.wait_for(task, timeout=2.0)
    assert manager.devices == [good]
    stuck.close.assert_awaited_once_with(code=1011)
    good.send_bytes.assert_awaited_once_with(b"\x01\x02")
    assert "Failed to send to WebSocket; removing connection (role=device, timeout)" in caplog.text


@pytest.mark.asyncio
async def test_double_removal_is_idempotent():
    """§1: 送信失敗で外した後に受信ループの disconnect() が走っても問題ない。"""
    manager = ConnectionManager()
    bad = make_mock_ws()
    bad.send_text.side_effect = RuntimeError("closed")
    other = make_mock_ws()
    await manager.connect(bad, "device")
    await manager.connect(other, "device")
    await manager.send_to_devices("x")
    await manager.disconnect(bad, "device")
    await manager.disconnect(bad, "device")
    assert manager.devices == [other]


@pytest.mark.asyncio
async def test_same_host_device_replaces_stale_connection(caplog):
    """§2: 同じ host の device が新しく接続すると、古い device が外されて close(code=1012) される。"""
    manager = ConnectionManager()
    old = make_ws_from("192.168.1.50")
    new = make_ws_from("192.168.1.50")
    await manager.connect(old, "device")
    with caplog.at_level(logging.INFO):
        await manager.connect(new, "device")
    assert manager.devices == [new]
    old.close.assert_awaited_once_with(code=1012)
    new.close.assert_not_awaited()
    assert "Replaced stale device connection from 192.168.1.50" in caplog.text


@pytest.mark.asyncio
async def test_same_host_media_replaces_stale_connection(caplog):
    manager = ConnectionManager()
    old = make_ws_from("192.168.1.50")
    new = make_ws_from("192.168.1.50")
    await manager.connect(old, "media")
    with caplog.at_level(logging.INFO):
        await manager.connect(new, "media")
    assert manager.media == [new]
    old.close.assert_awaited_once_with(code=1012)
    assert "Replaced stale media connection from 192.168.1.50" in caplog.text


@pytest.mark.asyncio
async def test_different_host_device_is_kept():
    """§2: 送信元 IP が違う device (シミュレータと実機) は残る。"""
    manager = ConnectionManager()
    real = make_ws_from("192.168.1.50")
    sim = make_ws_from("127.0.0.1")
    await manager.connect(real, "device")
    await manager.connect(sim, "device")
    assert manager.devices == [real, sim]
    real.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_host_other_role_is_not_replaced():
    """§2: 置き換えは同じ役割の中だけ (device の接続は media を外さない)。"""
    manager = ConnectionManager()
    media = make_ws_from("192.168.1.50")
    device = make_ws_from("192.168.1.50")
    await manager.connect(media, "media")
    await manager.connect(device, "device")
    assert manager.media == [media]
    assert manager.devices == [device]
    media.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_host_app_is_not_replaced():
    """§2: app は置き換えない (同じ PC で複数のタブを開ける)。"""
    manager = ConnectionManager()
    tab1 = make_ws_from("127.0.0.1")
    tab2 = make_ws_from("127.0.0.1")
    await manager.connect(tab1, "app")
    await manager.connect(tab2, "app")
    assert manager.apps == [tab1, tab2]
    tab1.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_client_is_not_replaced():
    """§2: ws.client が無い接続は置き換えない。"""
    manager = ConnectionManager()
    a = make_mock_ws()
    a.client = None
    b = make_mock_ws()
    b.client = None
    await manager.connect(a, "device")
    await manager.connect(b, "device")
    assert manager.devices == [a, b]
    a.close.assert_not_awaited()
