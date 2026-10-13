import logging

logger = logging.getLogger(__name__)


class AnimState:
    """Hub 全体で単一の発話アニメーション (口パク) フラグ。

    ListenState と異なり、音声パイプラインの purge は不要 (表示のみの切替であり、
    STT/VAD の滞留状態には影響しない)。
    """

    def __init__(self, mouth: bool = True) -> None:
        self.mouth = mouth
