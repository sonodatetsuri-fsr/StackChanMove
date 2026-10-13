"""VLM バックエンド (stackchan_move ``services/vlm.py`` のコピー)。

カメラ画像と文脈テキストを与えて応答を返す汎用サービス。バックエンドは
Ollama (``/api/chat``、``images`` に base64、``think: false``、``keep_alive``、
``options{temperature,num_ctx,num_predict}``。``schema`` を渡せば ``format`` = JSON Schema)。
プロンプトの中身は呼び出し側 (``services/vlm_qa.py``) が持ち、本モジュールは持たない。

stackchan_move からの変更: 学習との GPU 排他 (``trainer`` 参照・学習中の拒否) と、
stackchan_move のクラウドバックエンド向けの属性を削った。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

# probe 結果のキャッシュ時間 [s]。
PROBE_CACHE_S = 30.0
# p50 を取る直近成功要求数。
STATS_WINDOW = 20
# probe (version / tags) と unload は軽い要求なので短い上限にする。
PROBE_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 5.0


class VlmError(Exception):
    """VLM 問い合わせの失敗 (基底)。"""


class VlmUnavailable(VlmError):
    """未構成・接続不能・HTTP エラー・応答不正。"""


class VlmTimeout(VlmError):
    """1 要求の上限時間 (``VLM_TIMEOUT_S``) 超過。"""


@dataclass
class VlmResult:
    """1 要求の結果。``data`` は schema 指定時に JSON 解釈できた dict、それ以外は None。

    ``wait_ms`` はロック待ちの時間 [ms]。バックエンド呼び出しの
    所要時間 (``ms``) には含めない。
    """

    text: str
    data: dict | None
    ms: float
    load_ms: float
    prompt_tokens: int
    eval_tokens: int
    model: str
    wait_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class VlmBackend(Protocol):
    """VLM バックエンド抽象。"""

    async def probe(self) -> dict[str, Any]: ...

    async def load(self) -> float: ...

    async def unload(self) -> None: ...

    async def chat(
        self,
        images: list[bytes],
        system: str,
        user: str,
        schema: dict | None,
        options: dict[str, Any],
    ) -> VlmResult: ...

    async def aclose(self) -> None: ...


# --- ヘルパ ----------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_keep_alive(value: str | float | int | None) -> float | None:
    """``keep_alive`` を秒に直す。負値は無期限 (None)、解釈できなければ Ollama 既定の 5 分とみなす。

    例: ``"10m"`` → 600、``"30s"`` → 30、``"1h"`` → 3600、``"300"`` → 300、``"-1"`` → None、``"0"`` → 0。
    """
    if value is None:
        return 300.0
    if isinstance(value, int | float):
        seconds = float(value)
    else:
        m = _DURATION_RE.match(str(value))
        if m is None:
            return 300.0
        seconds = float(m.group(1)) * _UNIT_S[(m.group(2) or "s").lower()]
    if seconds < 0:
        return None
    return seconds


def keep_alive_wire(value: str | float | int) -> str | float | int:
    """Ollama に送る ``keep_alive``。単位なしの数値文字列は数値 (秒) にする (Go の Duration は単位必須のため)。"""
    if isinstance(value, int | float):
        return value
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse_version(text: str) -> tuple[int, ...]:
    """``"0.33.1"`` / ``"0.17.1-rc2"`` → ``(0, 33, 1)``。数字部分だけを比較に使う。"""
    parts: list[int] = []
    for piece in str(text).strip().lstrip("v").split("."):
        m = re.match(r"\d+", piece)
        if m is None:
            break
        parts.append(int(m.group(0)))
    return tuple(parts)


def version_at_least(version: str, minimum: str) -> bool:
    have = parse_version(version)
    need = parse_version(minimum)
    if not have:
        return False
    width = max(len(have), len(need))
    return have + (0,) * (width - len(have)) >= need + (0,) * (width - len(need))


def normalize_tag(tag: str) -> str:
    """タグ省略時は ``:latest`` を補う (``qwen3.5`` → ``qwen3.5:latest``)。"""
    tag = str(tag).strip()
    name = tag.rsplit("/", 1)[-1]
    return tag if ":" in name else f"{tag}:latest"


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    text = response.text.strip()
    return text[:200] if text else response.reason_phrase


# --- Ollama ---------------------------------------------------------------


class OllamaBackend:
    """Ollama HTTP API のバックエンド。"""

    name = "ollama"

    def __init__(
        self,
        url: str,
        model: str,
        *,
        timeout_s: float = 60.0,
        keep_alive: str | float | int = "10m",
        num_ctx: int = 8192,
        temperature: float = 0.2,
        num_predict: int = 400,
        min_version: str = "0.17.1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.keep_alive = keep_alive
        self.num_ctx = int(num_ctx)
        self.temperature = float(temperature)
        self.num_predict = int(num_predict)
        self.min_version = min_version
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(self.timeout_s, connect=CONNECT_TIMEOUT_S),
            transport=transport,
        )

    def default_options(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "num_ctx": self.num_ctx, "num_predict": self.num_predict}

    async def _request(self, method: str, path: str, *, json_body: Any = None, timeout: float | None = None) -> Any:
        """HTTP 要求を送り JSON を返す。失敗は VlmTimeout / VlmUnavailable に変換する。"""
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT_S, timeout))
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise VlmTimeout(f"ollama {path} timed out: {exc.__class__.__name__}") from exc
        except httpx.HTTPError as exc:
            raise VlmUnavailable(f"ollama unreachable at {self.url}: {exc}") from exc
        if not response.is_success:
            raise VlmUnavailable(f"ollama {path} HTTP {response.status_code}: {_error_text(response)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise VlmUnavailable(f"ollama {path} returned invalid JSON") from exc
        if isinstance(body, dict) and body.get("error"):
            raise VlmUnavailable(f"ollama {path} error: {body['error']}")
        return body

    async def probe(self) -> dict[str, Any]:
        """``GET /api/version`` と ``GET /api/tags`` で稼働・版・モデル有無を確かめる (例外は投げない)。"""
        result: dict[str, Any] = {"ok": False, "version": None, "model_present": False, "message": ""}
        try:
            body = await self._request("GET", "/api/version", timeout=PROBE_TIMEOUT_S)
        except VlmError as exc:
            result["message"] = str(exc)
            return result
        version = str(body.get("version", "")) if isinstance(body, dict) else ""
        result["version"] = version or None
        try:
            tags = await self._request("GET", "/api/tags", timeout=PROBE_TIMEOUT_S)
        except VlmError as exc:
            result["message"] = str(exc)
            return result
        want = normalize_tag(self.model)
        models = tags.get("models", []) if isinstance(tags, dict) else []
        present = any(
            isinstance(m, dict) and any(normalize_tag(m.get(k) or "") == want for k in ("name", "model") if m.get(k))
            for m in models
        )
        result["model_present"] = present
        if not version_at_least(version, self.min_version):
            result["message"] = (
                f"ollama {version or 'unknown'} is older than {self.min_version} "
                "(update: curl -fsSL https://ollama.com/install.sh | sh)"
            )
            return result
        if not present:
            result["message"] = f"model {self.model} not found (run: ollama pull {self.model})"
            return result
        result["ok"] = True
        result["message"] = f"ollama {version} ok, model {self.model} present"
        return result

    async def load(self) -> float:
        """空 messages の chat でモデルを VRAM に載せる。ロードに要した ms (``load_duration``) を返す。"""
        body = await self._request(
            "POST",
            "/api/chat",
            json_body={"model": self.model, "messages": [], "keep_alive": keep_alive_wire(self.keep_alive)},
        )
        return float(body.get("load_duration") or 0) / 1e6 if isinstance(body, dict) else 0.0

    async def unload(self) -> None:
        """``keep_alive: 0`` でモデルを VRAM から解放する。"""
        await self._request(
            "POST",
            "/api/chat",
            json_body={"model": self.model, "messages": [], "keep_alive": 0},
            timeout=PROBE_TIMEOUT_S * 2,
        )

    def build_chat_body(
        self,
        images: list[bytes],
        system: str,
        user: str,
        schema: dict | None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_msg: dict[str, Any] = {"role": "user", "content": user}
        if images:
            user_msg["images"] = [base64.b64encode(img).decode("ascii") for img in images]
        messages.append(user_msg)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": keep_alive_wire(self.keep_alive),
            "options": {**self.default_options(), **(options or {})},
        }
        if schema is not None:
            body["format"] = schema
        return body

    async def chat(
        self,
        images: list[bytes],
        system: str,
        user: str,
        schema: dict | None,
        options: dict[str, Any] | None = None,
    ) -> VlmResult:
        payload = self.build_chat_body(images, system, user, schema, options)
        started = time.monotonic()
        body = await self._request("POST", "/api/chat", json_body=payload)
        ms = (time.monotonic() - started) * 1000.0
        if not isinstance(body, dict):
            raise VlmUnavailable("ollama /api/chat returned a non-object")
        message = body.get("message")
        text = str(message.get("content") or "") if isinstance(message, dict) else ""
        data: dict | None = None
        if schema is not None:
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            data = parsed if isinstance(parsed, dict) else None
        return VlmResult(
            text=text,
            data=data,
            ms=round(ms, 1),
            load_ms=round(float(body.get("load_duration") or 0) / 1e6, 1),
            prompt_tokens=int(body.get("prompt_eval_count") or 0),
            eval_tokens=int(body.get("eval_count") or 0),
            model=str(body.get("model") or self.model),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def build_backend(settings: Any) -> VlmBackend | None:
    """設定からバックエンドを作る。``vlm_backend`` が ollama 以外なら None (無効)。"""
    if getattr(settings, "vlm_backend", "none") != "ollama":
        return None
    return OllamaBackend(
        settings.vlm_url,
        settings.vlm_model,
        timeout_s=settings.vlm_timeout_s,
        keep_alive=settings.vlm_keep_alive,
        num_ctx=settings.vlm_num_ctx,
        temperature=settings.vlm_temperature,
        num_predict=settings.vlm_num_predict,
        min_version=settings.vlm_min_version,
    )


# --- サービス ---------------------------------------------------------------


class VlmService:
    """Hub 内の VLM 窓口。同時 1 要求に直列化し、統計を ``status()`` で出す。"""

    def __init__(
        self,
        settings: Any,
        *,
        backend: VlmBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self.backend: VlmBackend | None = backend if backend is not None else build_backend(settings)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._keep_alive_s = parse_keep_alive(getattr(settings, "vlm_keep_alive", "10m"))

        self.available: bool = False
        self.version: str | None = None
        self.model_present: bool = False
        self.message: str = "" if self.backend is not None else "vlm is not configured"
        self._probed_at: float | None = None

        self.requests: int = 0
        self.errors: int = 0  # 連続失敗数
        self.failures: int = 0  # 総失敗数
        self.last_ms: float | None = None
        self.last_error: str | None = None
        self._ms_window: deque[float] = deque(maxlen=STATS_WINDOW)
        self._last_response_at: float | None = None

    # --- 状態 ---------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        if self.backend is None:
            return "none"
        return str(getattr(self.backend, "name", getattr(self._settings, "vlm_backend", "ollama")))

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def loaded(self) -> bool:
        """直近の成功応答から keep_alive 以内 (unload 後は False)。"""
        if self._last_response_at is None:
            return False
        if self._keep_alive_s is None:
            return True
        return (self._clock() - self._last_response_at) <= self._keep_alive_s

    @property
    def p50_ms(self) -> float | None:
        if not self._ms_window:
            return None
        return round(float(statistics.median(self._ms_window)), 1)

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.backend_name,
            "url": getattr(self.backend, "url", None) if self.backend is not None else None,
            "model": getattr(self.backend, "model", None) if self.backend is not None else None,
            "available": self.available,
            "version": self.version,
            "model_present": self.model_present,
            "loaded": self.loaded,
            "busy": self.busy,
            "requests": self.requests,
            "errors": self.errors,
            "failures": self.failures,
            "last_ms": self.last_ms,
            "p50_ms": self.p50_ms,
            "last_error": self.last_error,
            "message": self.message,
        }

    async def refresh(self, force: bool = False) -> bool:
        """probe を 30 s キャッシュで実行し ``available`` を返す。"""
        if self.backend is None:
            self.available = False
            return False
        now = self._clock()
        if not force and self._probed_at is not None and (now - self._probed_at) < PROBE_CACHE_S:
            return self.available
        self._probed_at = now
        try:
            result = await self.backend.probe()
        except Exception as exc:  # probe は例外を投げない契約だが、実装差を吸収する
            result = {"ok": False, "version": None, "model_present": False, "message": f"probe failed: {exc}"}
        self.available = bool(result.get("ok"))
        self.version = result.get("version")
        self.model_present = bool(result.get("model_present"))
        self.message = str(result.get("message") or "")
        return self.available

    # --- 要求 ---------------------------------------------------------------

    def _refusal(self) -> str | None:
        if self.backend is None:
            return "vlm is not configured"
        return None

    async def ensure_loaded(self) -> float:
        """モデルを VRAM に載せる (ウォームアップ)。ロードに要した ms を返す。"""
        reason = self._refusal()
        if reason is not None:
            raise VlmUnavailable(reason)
        assert self.backend is not None
        async with self._lock:
            try:
                load_ms = await asyncio.wait_for(self.backend.load(), timeout=self._timeout_s)
            except TimeoutError as exc:
                raise VlmTimeout(f"vlm load timed out after {self._timeout_s:.0f}s") from exc
            self._last_response_at = self._clock()
            return float(load_ms or 0.0)

    @property
    def _timeout_s(self) -> float:
        return float(getattr(self._settings, "vlm_timeout_s", 60.0))

    async def ask(
        self,
        images: list[bytes],
        *,
        system: str = "",
        user: str,
        schema: dict | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> VlmResult:
        """画像 (空可) + system/user (+ schema) で 1 回問い合わせる。同時 1 要求に直列化する。

        ``timeout_s`` を指定すると、**ロックを取った後の** バックエンド
        呼び出しだけにその上限をかける。ロック待ちの時間は上限に含めない。省略時 (None) は
        ``VLM_TIMEOUT_S`` を使う。
        """
        reason = self._refusal()
        if reason is not None:
            raise VlmUnavailable(reason)
        assert self.backend is not None
        options: dict[str, Any] = {}
        if max_tokens is not None:
            options["num_predict"] = int(max_tokens)
        timeout = self._timeout_s if timeout_s is None else float(timeout_s)
        wait_started = self._clock()
        async with self._lock:
            wait_ms = round(max(0.0, (self._clock() - wait_started) * 1000.0), 1)
            self.requests += 1
            try:
                result = await asyncio.wait_for(
                    self.backend.chat(list(images), system, user, schema, options),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                self._record_failure(f"vlm request timed out after {timeout:.0f}s")
                raise VlmTimeout(self.last_error or "timeout") from exc
            except VlmError as exc:
                self._record_failure(str(exc))
                raise
            except Exception as exc:
                self._record_failure(f"vlm request failed: {exc}")
                raise VlmUnavailable(self.last_error or str(exc)) from exc
            result.wait_ms = wait_ms
            self.errors = 0
            self.last_ms = result.ms
            self._ms_window.append(result.ms)
            self._last_response_at = self._clock()
            return result

    def _record_failure(self, message: str) -> None:
        self.errors += 1
        self.failures += 1
        self.last_error = message
        logger.warning("VLM request failed (%d consecutive): %s", self.errors, message)

    async def unload(self) -> None:
        """モデルを VRAM から解放する。失敗はログのみ。"""
        self._last_response_at = None
        if self.backend is None:
            return
        try:
            await self.backend.unload()
            logger.info("VLM model unloaded")
        except Exception as exc:
            logger.warning("VLM unload failed: %s", exc)

    async def close(self) -> None:
        if self.backend is None:
            return
        with contextlib.suppress(Exception):
            await self.backend.aclose()
