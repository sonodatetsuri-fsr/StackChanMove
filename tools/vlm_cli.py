#!/usr/bin/env python3
"""VLM 単発問い合わせ。Hub 無しで ``OllamaBackend`` に既定のシステムプロンプトで質問し、回答と ms を出す。

プロンプトとモデルの試行用 (stackchan_move の ``tools/vlm_cli.py`` を質問応答用に縮めたもの)。
回答は Hub と同じく Markdown の記号を除いて表示する。``--n`` で同じ画像に複数回質問する (揺れの確認)。

Example::

    uv run python tools/vlm_cli.py --image data/photos/desk.jpg --question "何が見えますか?"
    uv run python tools/vlm_cli.py --image data/photos/desk.jpg --question "机の上には何がありますか" --n 3
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openmovehub.config import settings  # noqa: E402
from openmovehub.services.vlm import OllamaBackend, VlmError, VlmResult  # noqa: E402
from openmovehub.services.vlm_qa import clean_answer  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    image_path = Path(args.image)
    if not image_path.is_file():
        print(f"image not found: {image_path}", file=sys.stderr)
        return 2
    image = image_path.read_bytes()

    backend = OllamaBackend(
        args.url,
        args.model,
        timeout_s=settings.vlm_timeout_s,
        keep_alive=settings.vlm_keep_alive,
        num_ctx=settings.vlm_num_ctx,
        temperature=settings.vlm_temperature,
        num_predict=settings.vlm_num_predict,
        min_version=settings.vlm_min_version,
    )
    results: list[VlmResult] = []
    try:
        probe = await backend.probe()
        print(f"probe: {probe['message']}")
        if not probe["ok"]:
            return 1
        for i in range(1, args.n + 1):
            result = await backend.chat([image], settings.vlm_qa_system_prompt, args.question, None)
            results.append(result)
            print(
                f"--- {i}/{args.n}: ms={result.ms:.0f} load_ms={result.load_ms:.0f} "
                f"prompt_tokens={result.prompt_tokens} eval_tokens={result.eval_tokens} model={result.model}"
            )
            print(clean_answer(result.text))
    except VlmError as exc:
        print(f"VLM error: {exc}", file=sys.stderr)
        return 1
    finally:
        await backend.aclose()

    ms = [r.ms for r in results]
    print(f"=== {len(results)} requests: ms min={min(ms):.0f} median={statistics.median(ms):.0f} max={max(ms):.0f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask the VLM (Ollama) a question about one image without the Hub.")
    parser.add_argument("--image", required=True, help="JPEG/PNG file")
    parser.add_argument("--question", required=True, help="question (the system prompt is VLM_QA_SYSTEM_PROMPT)")
    parser.add_argument("--n", type=int, default=1, help="number of repeated requests")
    parser.add_argument("--url", default=settings.vlm_url, help="Ollama URL (default: VLM_URL)")
    parser.add_argument("--model", default=settings.vlm_model, help="model tag (default: VLM_MODEL)")
    args = parser.parse_args(argv)
    if args.n < 1:
        parser.error("--n must be >= 1")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
