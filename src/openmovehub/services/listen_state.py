import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ListenState:
    """Hub 全体で単一の listening フラグ。

    状態 (greeting / ``control listen`` / ``state listening:*``) を持つ。音声を受ける接続
    (``routers/ws.py``) は、OFF 遷移時に接続ごとの VAD 滞留分を破棄する purge
    コールバックを登録する。
    """

    def __init__(self, on: bool = True) -> None:
        self.on = on
        self._purge_callbacks: list[Callable[[], None]] = []

    def register_purge(self, callback: Callable[[], None]) -> None:
        self._purge_callbacks.append(callback)

    def unregister_purge(self, callback: Callable[[], None]) -> None:
        if callback in self._purge_callbacks:
            self._purge_callbacks.remove(callback)

    def purge(self) -> None:
        for callback in list(self._purge_callbacks):
            callback()
