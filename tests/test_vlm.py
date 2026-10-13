"""VLM バックエンド (``openmovehub.services.vlm``) のテスト (stackchan_move から移植)。

``OllamaBackend`` は ``httpx.MockTransport`` で模擬する。実 Ollama への統合テストは
``VLM_INTEGRATION=1`` のときだけ実行する (unload は他利用者に影響するので送らない)。
学習との排他 (``trainer``)・クラウド向け属性・``vlm_cli`` の分布表の検査は削った。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from openmovehub.config import DEFAULT_VLM_QA_SYSTEM_PROMPT, Settings
from openmovehub.services.vlm import (
    OllamaBackend,
    VlmBackend,
    VlmResult,
    VlmService,
    VlmTimeout,
    VlmUnavailable,
    keep_alive_wire,
    normalize_tag,
    parse_keep_alive,
    parse_version,
    version_at_least,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "obstacle_ahead": {"type": "boolean"}},
    "required": ["summary", "obstacle_ahead"],
}


class FakeOllama:
    """``/api/version`` ``/api/tags`` ``/api/chat`` を模擬し、受けた要求を記録する。"""

    def __init__(
        self,
        *,
        version: str = "0.33.1",
        models: list[dict] | None = None,
        content: str = '{"summary": "desk", "obstacle_ahead": true}',
        chat_status: int = 200,
        chat_body: Any = None,
    ) -> None:
        self.version = version
        self.models = models if models is not None else [{"name": "qwen3.5:9b", "model": "qwen3.5:9b"}]
        self.content = content
        self.chat_status = chat_status
        self.chat_body = chat_body
        self.requests: list[tuple[str, str, Any]] = []

    def chat_bodies(self) -> list[dict]:
        return [body for method, path, body in self.requests if path == "/api/chat"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": self.models})
        if request.url.path == "/api/chat":
            if self.chat_status != 200:
                return httpx.Response(self.chat_status, json={"error": "model 'x' not found"})
            if self.chat_body is not None:
                return httpx.Response(200, content=self.chat_body)
            if not body.get("messages"):
                return httpx.Response(
                    200, json={"model": body["model"], "message": {"role": "assistant", "content": ""}, "done": True}
                )
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "message": {"role": "assistant", "content": self.content},
                    "done": True,
                    "load_duration": 3_000_000_000,
                    "prompt_eval_count": 159,
                    "eval_count": 190,
                },
            )
        return httpx.Response(404, json={"error": "not found"})


def make_backend(fake: FakeOllama, **kwargs) -> OllamaBackend:
    return OllamaBackend(
        "http://ollama.test", kwargs.pop("model", "qwen3.5:9b"), transport=httpx.MockTransport(fake.handler), **kwargs
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeBackend:
    """VlmService 用の最小バックエンド。chat の遅延・失敗を制御できる。"""

    name = "fake"
    url = "http://fake"
    model = "fake:1b"

    def __init__(self) -> None:
        self.probes = 0
        self.probe_result = {"ok": True, "version": "0.33.1", "model_present": True, "message": "ok"}
        self.delay = 0.0
        self.fail: Exception | None = None
        self.ms_seq: list[float] = []
        self.active = 0
        self.max_active = 0
        self.calls: list[dict] = []
        self.unloads = 0
        self.closed = False

    async def probe(self) -> dict:
        self.probes += 1
        return dict(self.probe_result)

    async def load(self) -> float:
        return 3000.0

    async def unload(self) -> None:
        self.unloads += 1
        if self.fail is not None:
            raise self.fail

    async def chat(self, images, system, user, schema, options) -> VlmResult:
        self.calls.append({"images": images, "system": system, "user": user, "schema": schema, "options": options})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail is not None:
                raise self.fail
        finally:
            self.active -= 1
        ms = self.ms_seq.pop(0) if self.ms_seq else 100.0
        return VlmResult(text="{}", data={}, ms=ms, load_ms=0.0, prompt_tokens=1, eval_tokens=1, model=self.model)

    async def aclose(self) -> None:
        self.closed = True


def make_service(backend=None, *, clock=None, **settings_kwargs) -> VlmService:
    settings = Settings(**settings_kwargs)
    return VlmService(settings, backend=backend or FakeBackend(), clock=clock or Clock())


# --- ヘルパ ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10m", 600.0),
        ("30s", 30.0),
        ("1h", 3600.0),
        ("300", 300.0),
        (120, 120.0),
        ("-1", None),
        ("-1m", None),
        ("0", 0.0),
        ("garbage", 300.0),
    ],
)
def test_parse_keep_alive(value, expected):
    assert parse_keep_alive(value) == expected


def test_keep_alive_wire_converts_plain_numbers():
    assert keep_alive_wire("10m") == "10m"
    assert keep_alive_wire("300") == 300
    assert keep_alive_wire("-1") == -1
    assert keep_alive_wire(0) == 0


def test_version_helpers():
    assert parse_version("0.33.1") == (0, 33, 1)
    assert parse_version("v0.17.1-rc2") == (0, 17, 1)
    assert version_at_least("0.33.1", "0.17.1")
    assert version_at_least("0.17.1", "0.17.1")
    assert version_at_least("1.0", "0.17.1")
    assert not version_at_least("0.17.0", "0.17.1")
    assert not version_at_least("", "0.17.1")


def test_normalize_tag():
    assert normalize_tag("qwen3.5") == "qwen3.5:latest"
    assert normalize_tag("qwen3.5:9b") == "qwen3.5:9b"
    assert normalize_tag("registry.local:5000/lib/qwen") == "registry.local:5000/lib/qwen:latest"


def test_ollama_backend_satisfies_protocol():
    backend = OllamaBackend("http://x", "m")
    assert isinstance(backend, VlmBackend)
    assert backend.name == "ollama"


def test_settings_defaults():
    s = Settings()
    assert s.vlm_backend == "ollama"
    assert s.vlm_url == "http://127.0.0.1:11434"
    assert s.vlm_model == "qwen3.5:9b"
    assert s.vlm_timeout_s == 60
    assert s.vlm_keep_alive == "10m"
    assert s.vlm_num_ctx == 8192
    assert s.vlm_temperature == 0.2
    assert s.vlm_num_predict == 400
    assert s.vlm_min_version == "0.17.1"


def test_env_vlm_backend_unknown_is_none(monkeypatch):
    from openmovehub.config import _env_vlm_backend

    monkeypatch.setenv("VLM_BACKEND", "vllm")
    assert _env_vlm_backend("VLM_BACKEND") == "none"
    monkeypatch.setenv("VLM_BACKEND", " Ollama ")
    assert _env_vlm_backend("VLM_BACKEND") == "ollama"
    monkeypatch.delenv("VLM_BACKEND")
    assert _env_vlm_backend("VLM_BACKEND") == "ollama"


# --- OllamaBackend -----------------------------------------------------------


async def test_probe_ok():
    fake = FakeOllama()
    backend = make_backend(fake)
    result = await backend.probe()
    await backend.aclose()
    assert result["ok"] is True
    assert result["version"] == "0.33.1"
    assert result["model_present"] is True
    assert [p for _, p, _ in fake.requests] == ["/api/version", "/api/tags"]


async def test_probe_old_version_is_not_ok():
    backend = make_backend(FakeOllama(version="0.16.3"))
    result = await backend.probe()
    await backend.aclose()
    assert result["ok"] is False
    assert result["version"] == "0.16.3"
    assert "older than 0.17.1" in result["message"]
    assert "install.sh" in result["message"]


async def test_probe_model_missing():
    backend = make_backend(FakeOllama(models=[{"name": "gemma4:12b", "model": "gemma4:12b"}]))
    result = await backend.probe()
    await backend.aclose()
    assert result["ok"] is False
    assert result["model_present"] is False
    assert "ollama pull qwen3.5:9b" in result["message"]


async def test_probe_matches_model_key_and_latest_suffix():
    fake = FakeOllama(models=[{"name": "", "model": "llava:latest"}])
    backend = make_backend(fake, model="llava")
    assert (await backend.probe())["model_present"] is True
    await backend.aclose()
    # 完全一致でないタグ (9b 対 27b) は一致させない。
    backend = make_backend(FakeOllama(models=[{"name": "qwen3.5:27b"}]))
    assert (await backend.probe())["model_present"] is False
    await backend.aclose()


async def test_probe_unreachable():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    backend = OllamaBackend("http://ollama.test", "qwen3.5:9b", transport=httpx.MockTransport(refuse))
    result = await backend.probe()
    await backend.aclose()
    assert result["ok"] is False
    assert result["version"] is None
    assert "unreachable" in result["message"]


async def test_chat_sends_images_schema_think_keep_alive_options():
    fake = FakeOllama()
    backend = make_backend(fake, keep_alive="10m", num_ctx=8192, temperature=0.2, num_predict=400)
    result = await backend.chat([b"\xff\xd8jpeg"], "you are a robot", "what is ahead?", SCHEMA, {})
    await backend.aclose()

    body = fake.chat_bodies()[0]
    assert body["model"] == "qwen3.5:9b"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == SCHEMA
    assert body["keep_alive"] == "10m"
    assert body["options"] == {"temperature": 0.2, "num_ctx": 8192, "num_predict": 400}
    assert body["messages"][0] == {"role": "system", "content": "you are a robot"}
    user = body["messages"][1]
    assert user["role"] == "user"
    assert user["content"] == "what is ahead?"
    assert user["images"] == [base64.b64encode(b"\xff\xd8jpeg").decode("ascii")]

    assert result.data == {"summary": "desk", "obstacle_ahead": True}
    assert result.text == '{"summary": "desk", "obstacle_ahead": true}'
    assert result.load_ms == 3000.0
    assert result.prompt_tokens == 159
    assert result.eval_tokens == 190
    assert result.model == "qwen3.5:9b"
    assert result.ms >= 0
    assert set(result.to_dict()) == {
        "text",
        "data",
        "ms",
        "load_ms",
        "prompt_tokens",
        "eval_tokens",
        "model",
        "wait_ms",
    }


async def test_chat_text_only_omits_images_system_and_format():
    fake = FakeOllama(content="hello")
    backend = make_backend(fake)
    result = await backend.chat([], "", "say hello", None, {"num_predict": 20})
    await backend.aclose()

    body = fake.chat_bodies()[0]
    assert body["messages"] == [{"role": "user", "content": "say hello"}]
    assert "format" not in body
    assert body["options"]["num_predict"] == 20
    assert result.text == "hello"
    assert result.data is None


async def test_chat_non_json_content_gives_data_none():
    backend = make_backend(FakeOllama(content="I think the floor is clear."))
    result = await backend.chat([b"x"], "", "q", SCHEMA, {})
    await backend.aclose()
    assert result.data is None
    assert result.text == "I think the floor is clear."


async def test_chat_json_array_content_gives_data_none():
    backend = make_backend(FakeOllama(content="[1, 2]"))
    result = await backend.chat([b"x"], "", "q", SCHEMA, {})
    await backend.aclose()
    assert result.data is None


async def test_chat_http_error_raises_unavailable_with_message():
    backend = make_backend(FakeOllama(chat_status=404))
    with pytest.raises(VlmUnavailable, match="HTTP 404: model 'x' not found"):
        await backend.chat([b"x"], "", "q", None, {})
    await backend.aclose()


async def test_chat_invalid_response_json_raises_unavailable():
    backend = make_backend(FakeOllama(chat_body=b"<html>oops</html>"))
    with pytest.raises(VlmUnavailable, match="invalid JSON"):
        await backend.chat([b"x"], "", "q", None, {})
    await backend.aclose()


async def test_chat_timeout_raises_vlm_timeout():
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    backend = OllamaBackend("http://ollama.test", "qwen3.5:9b", transport=httpx.MockTransport(slow))
    with pytest.raises(VlmTimeout):
        await backend.chat([b"x"], "", "q", None, {})
    await backend.aclose()


async def test_chat_connect_error_raises_unavailable():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    backend = OllamaBackend("http://ollama.test", "qwen3.5:9b", transport=httpx.MockTransport(refuse))
    with pytest.raises(VlmUnavailable, match="unreachable"):
        await backend.chat([b"x"], "", "q", None, {})
    await backend.aclose()


async def test_load_and_unload_bodies():
    fake = FakeOllama()
    backend = make_backend(fake, keep_alive="10m")
    await backend.load()
    await backend.unload()
    await backend.aclose()
    load_body, unload_body = fake.chat_bodies()
    assert load_body == {"model": "qwen3.5:9b", "messages": [], "keep_alive": "10m"}
    assert unload_body == {"model": "qwen3.5:9b", "messages": [], "keep_alive": 0}


# --- VlmService --------------------------------------------------------------


def test_none_backend_is_not_configured():
    service = VlmService(Settings(vlm_backend="none"))
    assert service.backend is None
    status = service.status()
    assert status["backend"] == "none"
    assert status["available"] is False
    assert status["message"] == "vlm is not configured"


def test_settings_build_ollama_backend():
    service = VlmService(Settings(vlm_url="http://h:1/", vlm_model="m:1", vlm_keep_alive="30s"))
    assert isinstance(service.backend, OllamaBackend)
    assert service.backend.url == "http://h:1"
    assert service.backend.model == "m:1"
    assert service.backend.keep_alive == "30s"


async def test_none_backend_ask_raises_unavailable():
    service = VlmService(Settings(vlm_backend="none"))
    with pytest.raises(VlmUnavailable, match="vlm is not configured"):
        await service.ask([], user="q")
    assert await service.refresh() is False
    await service.unload()  # 例外を投げない
    await service.close()


async def test_status_keys():
    service = make_service()
    await service.refresh()
    status = service.status()
    assert {
        "backend",
        "url",
        "model",
        "available",
        "version",
        "model_present",
        "loaded",
        "busy",
        "requests",
        "errors",
        "last_ms",
        "p50_ms",
        "message",
    } <= set(status)
    assert status["available"] is True
    assert status["version"] == "0.33.1"
    assert status["backend"] == "fake"


async def test_refresh_is_cached_for_30_seconds():
    backend = FakeBackend()
    clock = Clock()
    service = make_service(backend, clock=clock)
    assert await service.refresh() is True
    clock.now += 29.0
    await service.refresh()
    assert backend.probes == 1
    clock.now += 2.0
    backend.probe_result = {"ok": False, "version": None, "model_present": False, "message": "down"}
    assert await service.refresh() is False
    assert backend.probes == 2
    await service.refresh(force=True)
    assert backend.probes == 3
    assert service.status()["message"] == "down"


async def test_ask_passes_arguments_and_max_tokens():
    backend = FakeBackend()
    service = make_service(backend)
    await service.ask([b"a", b"b"], system="sys", user="u", schema=SCHEMA, max_tokens=50)
    await service.ask([], user="text only")
    first, second = backend.calls
    assert first == {
        "images": [b"a", b"b"],
        "system": "sys",
        "user": "u",
        "schema": SCHEMA,
        "options": {"num_predict": 50},
    }
    assert second["images"] == [] and second["options"] == {} and second["schema"] is None


async def test_ask_is_serialized():
    backend = FakeBackend()
    backend.delay = 0.05
    service = make_service(backend)

    async def one():
        return await service.ask([b"x"], user="q")

    task = asyncio.gather(one(), one())
    await asyncio.sleep(0.01)
    assert service.status()["busy"] is True
    await task
    assert backend.max_active == 1
    assert service.status()["busy"] is False
    assert service.status()["requests"] == 2


async def test_ask_timeout_raises_vlm_timeout_and_counts_error():
    backend = FakeBackend()
    backend.delay = 1.0
    service = make_service(backend, vlm_timeout_s=0.05)
    with pytest.raises(VlmTimeout):
        await service.ask([b"x"], user="q")
    status = service.status()
    assert status["requests"] == 1
    assert status["errors"] == 1
    assert "timed out" in status["last_error"]


async def test_ask_timeout_s_excludes_lock_wait_and_sets_wait_ms():
    """timeout_s はロックを取った後のバックエンド呼び出しだけに掛かり、
    ロック待ちの時間は数えない。待たされた要求の ``wait_ms`` は待たされない要求より大きい。
    """
    backend = FakeBackend()
    backend.delay = 0.15
    # 実時間で計るので、make_service の固定 Clock() ではなく既定 (time.monotonic) を使う。
    service = VlmService(Settings(vlm_timeout_s=5.0), backend=backend)

    async def slow_holder():
        return await service.ask([b"x"], user="slow")

    task_a = asyncio.create_task(slow_holder())
    await asyncio.sleep(0.03)  # task_a はロックを取り backend.chat 内で sleep 中
    backend.delay = 0.0  # 待たされる側は自分の番になれば素早く終わる
    result_b = await service.ask([b"x"], user="q", timeout_s=0.05)
    result_a = await task_a

    # timeout_s (0.05s) はロック待ち (約 0.12s) を含まないのでタイムアウトしない。
    assert result_b.wait_ms > 50.0
    assert result_a.wait_ms < 5.0


async def test_ask_timeout_s_omitted_uses_vlm_timeout_s_default():
    """timeout_s 省略時は VLM_TIMEOUT_S (settings.vlm_timeout_s) で切れる。"""
    backend = FakeBackend()
    backend.delay = 1.0
    service = VlmService(Settings(vlm_timeout_s=0.05), backend=backend)
    with pytest.raises(VlmTimeout, match="after 0s"):
        await service.ask([b"x"], user="q")
    assert "timed out after 0s" in service.status()["last_error"]


async def test_ask_timeout_s_argument_times_out_and_records_failure():
    """timeout_s を指定してバックエンドがそれを超えたら VlmTimeout が上がり、
    failures / last_error が従来どおり更新される。"""
    backend = FakeBackend()
    backend.delay = 1.0
    service = VlmService(Settings(vlm_timeout_s=60.0), backend=backend)
    with pytest.raises(VlmTimeout, match="after 0s"):
        await service.ask([b"x"], user="q", timeout_s=0.05)
    status = service.status()
    assert status["errors"] == 1
    assert status["failures"] == 1
    assert "timed out after 0s" in status["last_error"]


async def test_statistics_p50_errors_requests():
    backend = FakeBackend()
    clock = Clock()
    service = make_service(backend, clock=clock, vlm_keep_alive="10m")
    backend.ms_seq = [100.0, 300.0, 200.0]
    for _ in range(3):
        await service.ask([b"x"], user="q")
    status = service.status()
    assert status["requests"] == 3
    assert status["errors"] == 0
    assert status["last_ms"] == 200.0
    assert status["p50_ms"] == 200.0
    assert status["loaded"] is True

    backend.fail = VlmUnavailable("ollama unreachable")
    for _ in range(2):
        with pytest.raises(VlmUnavailable):
            await service.ask([b"x"], user="q")
    status = service.status()
    assert status["requests"] == 5
    assert status["errors"] == 2  # 連続失敗数
    assert status["failures"] == 2
    assert status["last_error"] == "ollama unreachable"
    assert status["p50_ms"] == 200.0  # 失敗は p50 に入れない

    backend.fail = None
    await service.ask([b"x"], user="q")
    assert service.status()["errors"] == 0
    assert service.status()["failures"] == 2


async def test_p50_uses_last_20_successes():
    backend = FakeBackend()
    service = make_service(backend)
    backend.ms_seq = [10_000.0] * 5 + [float(i) for i in range(1, 21)]
    for _ in range(25):
        await service.ask([b"x"], user="q")
    assert service.status()["p50_ms"] == 10.5


async def test_unexpected_backend_exception_becomes_unavailable():
    backend = FakeBackend()
    backend.fail = RuntimeError("boom")
    service = make_service(backend)
    with pytest.raises(VlmUnavailable, match="boom"):
        await service.ask([b"x"], user="q")


async def test_loaded_expires_after_keep_alive_and_unload():
    backend = FakeBackend()
    clock = Clock()
    service = make_service(backend, clock=clock, vlm_keep_alive="30s")
    assert service.status()["loaded"] is False
    await service.ask([b"x"], user="q")
    clock.now += 29.0
    assert service.status()["loaded"] is True
    clock.now += 2.0
    assert service.status()["loaded"] is False

    await service.ask([b"x"], user="q")
    await service.unload()
    assert backend.unloads == 1
    assert service.status()["loaded"] is False


async def test_loaded_forever_with_negative_keep_alive():
    clock = Clock()
    service = make_service(clock=clock, vlm_keep_alive="-1")
    await service.ensure_loaded()
    clock.now += 1e6
    assert service.status()["loaded"] is True


async def test_unload_failure_is_swallowed():
    backend = FakeBackend()
    backend.fail = VlmUnavailable("down")
    service = make_service(backend)
    await service.unload()
    assert backend.unloads == 1


async def test_close_closes_backend():
    backend = FakeBackend()
    service = make_service(backend)
    await service.close()
    assert backend.closed is True


async def test_service_with_mock_transport_end_to_end():
    fake = FakeOllama()
    service = VlmService(Settings(), backend=make_backend(fake))
    assert await service.refresh() is True
    result = await service.ask([b"img"], user="q", schema=SCHEMA, max_tokens=64)
    assert result.data == {"summary": "desk", "obstacle_ahead": True}
    assert fake.chat_bodies()[0]["options"]["num_predict"] == 64
    await service.unload()
    assert fake.chat_bodies()[-1]["keep_alive"] == 0
    await service.close()


# --- 統合テスト (実 Ollama) --------------------------------------------------


@pytest.mark.skipif(os.getenv("VLM_INTEGRATION") != "1", reason="set VLM_INTEGRATION=1 to query the real Ollama")
async def test_integration_real_ollama_two_requests():
    settings = Settings(
        vlm_url=os.getenv("VLM_URL", "http://127.0.0.1:11434"), vlm_model=os.getenv("VLM_MODEL", "qwen3.5:9b")
    )
    service = VlmService(settings)
    try:
        assert await service.refresh(force=True), service.status()["message"]
        photos = sorted(p for p in (REPO_ROOT / "data" / "photos").glob("*") if p.suffix.lower() in (".jpg", ".jpeg"))
        images = [photos[0].read_bytes()] if photos else []
        # 質問応答と同じ形 (既定のシステムプロンプト・schema 無し)。
        first = await service.ask(images, system=DEFAULT_VLM_QA_SYSTEM_PROMPT, user="何が見えますか?")
        # 構造化出力 (schema) も使えること。
        second = await service.ask([], user="Reply with a short summary of a desk.", schema=SCHEMA)
        assert first.text.strip()
        assert first.eval_tokens > 0
        assert isinstance(second.data, dict)
        assert "summary" in second.data
        status = service.status()
        assert status["requests"] == 2
        assert status["errors"] == 0
        assert status["loaded"] is True
        print(f"integration: first ms={first.ms} load_ms={first.load_ms} text={first.text!r} / second ms={second.ms}")
    finally:
        # unload は他の利用者がいる可能性があるので送らない。
        await service.close()


# --- tools/vlm_cli.py -------------------------------------------------------


def test_vlm_cli_missing_image_returns_2(tmp_path):
    from vlm_cli import main as vlm_cli_main

    assert vlm_cli_main(["--image", str(tmp_path / "none.jpg"), "--question", "q"]) == 2


def test_vlm_cli_rejects_n_below_1(tmp_path):
    from vlm_cli import main as vlm_cli_main

    with pytest.raises(SystemExit):
        vlm_cli_main(["--image", str(tmp_path / "x.jpg"), "--question", "q", "--n", "0"])
