"""導入結果の検査 (``uv run python -m openmovehub.doctor``)。

Ubuntu / Windows 共通。セットアップスクリプト (``setup_ubuntu.sh`` / ``scripts/setup_windows.ps1``) の
最後と ``--check`` から呼ばれる。

    uv run python -m openmovehub.doctor [--json] [--skip-vlm] [--skip-firmware] [--prefetch-stt]

- 通常: 検査 (python / stt / tts / vlm / firmware / network) を順に実行して表を出す。
  FAIL が 1 つでもあれば exit 1、無ければ exit 0 (WARN は合格扱い)。
  ``SETUP OK`` などの最終判定の文言は呼び出し側のスクリプトが出す。
- ``--json``: 標準出力に JSON だけを出す (``{"ok": bool, "checks": [...]}``)。exit code は同じ。
- ``--prefetch-stt``: 検査はせず、``STT_PLUGIN=whisper`` のときだけ ``STT_MODEL`` を
  Hugging Face のキャッシュへ取得する。

設定は ``.env`` + 環境変数 (``openmovehub.config`` の import で ``.env`` が読まれる)。固定の版は
``scripts/versions.env`` から読む。外部呼び出し (subprocess・HTTP・faster-whisper・
ctranslate2) はテストで差し替えられるよう小さな関数に分けている。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

#: リポジトリのルート (``src/openmovehub/doctor.py`` から 2 つ上)。
REPO_ROOT = Path(__file__).resolve().parents[2]
#: 固定する版・URL (``KEY=VALUE`` の行)。
VERSIONS_ENV = REPO_ROOT / "scripts" / "versions.env"

Status = Literal["ok", "warn", "fail", "skip"]

#: VOICEVOX を一時起動したときに ``/version`` を待つ上限 [s]。
VOICEVOX_START_TIMEOUT_S = 60.0
#: VLM の問い合わせ上限の下限 [s] (初回ロード込み)。
VLM_MIN_TIMEOUT_S = 120.0
#: Windows ファイアウォール規則の表示名。
FIREWALL_RULE_NAME = "Open StackChan Move Hub"
#: TTS の試し合成に使う文。
TTS_TEST_TEXT = "テストです"

SETUP_HINT_UBUNTU = "bash setup_ubuntu.sh"
SETUP_HINT_WINDOWS = "setup_windows.bat"


@dataclass
class CheckResult:
    """1 検査の結果 (``--json`` の ``checks[]`` の 1 要素)。"""

    name: str
    status: Status
    detail: str
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- 共通ヘルパ -------------------------------------------------------------


def is_windows() -> bool:
    return sys.platform == "win32"


def setup_command() -> str:
    """OS ごとのセットアップの再実行コマンド (hint 用)。"""
    return SETUP_HINT_WINDOWS if is_windows() else SETUP_HINT_UBUNTU


def read_versions(path: Path = VERSIONS_ENV) -> dict[str, str]:
    """``versions.env`` を ``KEY=VALUE`` の行として読む (``#`` で始まる行と空行は無視)。

    ファイルが無ければ空の dict を返す。
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def run_command(cmd: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """コマンドを実行して ``(returncode, stdout+stderr)`` を返す。起動できなければ ``(-1, 理由)``。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return -1, f"{type(e).__name__}: {e}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _error_text(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


# --- 1. python ---------------------------------------------------------------


def check_python() -> CheckResult:
    """Python 3.12 で ``openmovehub`` が import できる。"""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info[:2] != (3, 12):
        return CheckResult(
            "python",
            "fail",
            f"Python {version} (3.12 が必要)",
            "uv run で実行してください (uv が Python 3.12 を用意します)。",
        )
    try:
        import openmovehub  # noqa: F401
        import openmovehub.config  # noqa: F401
    except Exception as e:
        return CheckResult("python", "fail", f"import openmovehub failed: {_error_text(e)}", "uv sync --frozen")
    return CheckResult("python", "ok", f"Python {version}, openmovehub importable")


# --- 2. stt ------------------------------------------------------------------


def stt_model_name() -> str:
    return os.getenv("STT_MODEL", "large-v3-turbo")


def _prepare_whisper_import() -> None:
    """faster-whisper / ctranslate2 の import 前に Windows の CUDA DLL ディレクトリを登録する。"""
    import openmovehub.plugins.stt_whisper  # noqa: F401


def whisper_model_cached(model: str) -> str:
    """モデルがローカルにあればそのパスを返す (無ければ例外)。ローカルパスはそのまま返す。"""
    if Path(model).exists():
        return model
    _prepare_whisper_import()
    from faster_whisper.utils import download_model

    return str(download_model(model, local_files_only=True))


def whisper_download(model: str) -> str:
    """モデルを Hugging Face のキャッシュへ取得してパスを返す。"""
    if Path(model).exists():
        return model
    _prepare_whisper_import()
    from faster_whisper.utils import download_model

    return str(download_model(model))


def cuda_device_count() -> int:
    """CUDA デバイス数 (取れなければ 0)。"""
    try:
        _prepare_whisper_import()
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def whisper_cuda_trial(model: str) -> str | None:
    """CUDA でロードして試し認識する。CPU にフォールバックしたら理由を、成功したら None を返す。"""
    from openmovehub.plugins.stt_whisper import WhisperStt

    stt = WhisperStt(model=model, device="cuda", compute_type="auto")
    try:
        stt.load()
        return stt.fallback
    finally:
        stt.unload()


def resolve_plugin(kind: Literal["stt", "tts"], name: str) -> Any:
    from openmovehub.plugins.registry import resolve_factory

    return resolve_factory(kind, name)


def _cuda_hint() -> str:
    hint = "CPU で動作します。GPU を使うには CUDA ライブラリを入れてください: uv sync --frozen --group cuda"
    if not is_windows():
        hint += (
            " (Linux は .venv 内の nvidia/cublas/lib と nvidia/cudnn/lib を LD_LIBRARY_PATH に追加。"
            "start_ubuntu.sh が自動で行います)"
        )
    return hint


def check_stt(settings: Any) -> CheckResult:
    """STT プラグインの解決、whisper ならモデルのキャッシュと CUDA での試しロード。"""
    name = settings.stt_plugin
    if name == "none":
        return CheckResult("stt", "skip", "STT_PLUGIN=none")
    try:
        resolve_plugin("stt", name)
    except Exception as e:
        return CheckResult(
            "stt",
            "fail",
            f"STT_PLUGIN={name} を解決できません: {_error_text(e)}",
            ".env の STT_PLUGIN を確認してください (whisper / none / 第三者プラグイン名)。",
        )
    if name != "whisper":
        return CheckResult("stt", "ok", f"STT_PLUGIN={name} resolved")
    model = stt_model_name()
    try:
        path = whisper_model_cached(model)
    except Exception as e:
        return CheckResult(
            "stt",
            "fail",
            f"whisper model {model} がキャッシュにありません ({type(e).__name__})",
            "uv run python -m openmovehub.doctor --prefetch-stt で取得してください。",
        )
    if cuda_device_count() <= 0:
        return CheckResult("stt", "ok", f"whisper {model} cached, device cpu ({path})")
    fallback = whisper_cuda_trial(model)
    if fallback is not None:
        return CheckResult("stt", "warn", f"whisper {model} cached, CUDA load failed: {fallback}", _cuda_hint())
    return CheckResult("stt", "ok", f"whisper {model} cached, CUDA load ok")


# --- 3. tts ------------------------------------------------------------------


def voicevox_url() -> str:
    return os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")


def voicevox_version(url: str, timeout: float = 3.0) -> str | None:
    """``GET <url>/version`` の版 (応答しなければ None)。"""
    try:
        resp = httpx.get(f"{url}/version", timeout=timeout)
        resp.raise_for_status()
        return str(resp.json())
    except Exception:
        return None


def voicevox_executable(version: str) -> Path:
    """``third_party/voicevox_engine/<版>/run`` (Windows は ``run.exe``)。"""
    exe = "run.exe" if is_windows() else "run"
    return REPO_ROOT / "third_party" / "voicevox_engine" / version / exe


def start_voicevox(exe: Path, port: int) -> subprocess.Popen:
    """VOICEVOX ENGINE を検査中だけ起動する (出力は ``logs/voicevox_doctor.log``)。"""
    log_path = REPO_ROOT / "logs" / "voicevox_doctor.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log: Any = open(log_path, "ab")  # noqa: SIM115  (Popen に渡して子プロセスが書く)
    except OSError:
        log = subprocess.DEVNULL
    try:
        return subprocess.Popen(
            [str(exe), "--host", "127.0.0.1", "--port", str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(exe.parent),
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def stop_process(proc: Any) -> None:
    """terminate して待ち、終わらなければ kill する。"""
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        pass


def wait_voicevox(url: str, proc: Any, timeout: float = VOICEVOX_START_TIMEOUT_S) -> str | None:
    """``/version`` が応答するまで待つ。プロセスが終了したか時間切れなら None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        version = voicevox_version(url, timeout=2.0)
        if version is not None:
            return version
        if proc.poll() is not None:
            return None
        time.sleep(1.0)
    return None


def voicevox_synthesize(url: str, text: str = TTS_TEST_TEXT) -> int:
    """VoicevoxTts で合成して PCM のバイト数を返す。"""
    from openmovehub.plugins.tts_voicevox import VoicevoxTts

    async def run() -> int:
        tts = VoicevoxTts(
            url=url,
            speaker=int(os.getenv("VOICEVOX_SPEAKER", "1")),
            speed=float(os.getenv("VOICEVOX_SPEED", "1.0")),
            timeout_s=float(os.getenv("TTS_TIMEOUT_S", "120")),
        )
        try:
            audio = await tts.synthesize(text)
        finally:
            await tts.close()
        return len(audio.pcm)

    return asyncio.run(run())


def _synth_result(url: str, version: str, expected: str, started: bool) -> CheckResult:
    try:
        size = voicevox_synthesize(url)
    except Exception as e:
        return CheckResult(
            "tts",
            "fail",
            f"VOICEVOX {version}: 合成に失敗しました: {_error_text(e)}",
            "VOICEVOX ENGINE のログ (logs/voicevox_doctor.log) を確認してください。",
        )
    if size <= 0:
        return CheckResult(
            "tts", "fail", f"VOICEVOX {version}: 合成結果が空です", "VOICEVOX_SPEAKER を確認してください。"
        )
    where = "temporarily started" if started else "running"
    detail = f"VOICEVOX {version} ({where}, {url}), synthesis {size} bytes"
    if version != expected:
        return CheckResult(
            "tts",
            "warn",
            detail + f", expected {expected}",
            f"起動中の VOICEVOX ENGINE の版が {expected} ではありません (動作はします)。",
        )
    return CheckResult("tts", "ok", detail)


def check_tts(settings: Any, versions: dict[str, str]) -> CheckResult:
    """VOICEVOX が応答すればそれで、しなければ ``third_party/`` の実行ファイルを一時起動して検査する。"""
    name = settings.tts_plugin
    if name == "none":
        return CheckResult("tts", "skip", "TTS_PLUGIN=none")
    if name != "voicevox":
        try:
            resolve_plugin("tts", name)
        except Exception as e:
            return CheckResult(
                "tts",
                "fail",
                f"TTS_PLUGIN={name} を解決できません: {_error_text(e)}",
                ".env の TTS_PLUGIN を確認してください (voicevox / none / 第三者プラグイン名)。",
            )
        return CheckResult("tts", "ok", f"TTS_PLUGIN={name} resolved")

    expected = versions.get("VOICEVOX_VERSION", "")
    url = voicevox_url()
    version = voicevox_version(url)
    if version is not None:
        return _synth_result(url, version, expected or version, started=False)

    if not expected:
        return CheckResult(
            "tts",
            "fail",
            f"VOICEVOX が {url} で応答せず、versions.env に VOICEVOX_VERSION がありません",
            f"セットアップを再実行してください: {setup_command()}",
        )
    exe = voicevox_executable(expected)
    if not exe.is_file():
        return CheckResult(
            "tts",
            "fail",
            f"VOICEVOX が {url} で応答せず、実行ファイル {exe} もありません",
            f"セットアップを再実行して VOICEVOX ENGINE {expected} を導入してください: {setup_command()}",
        )
    port = urlparse(url).port or int(versions.get("VOICEVOX_PORT", "50021"))
    local_url = f"http://127.0.0.1:{port}"
    proc = start_voicevox(exe, port)
    try:
        version = wait_voicevox(local_url, proc)
        if version is None:
            return CheckResult(
                "tts",
                "fail",
                f"VOICEVOX ENGINE ({exe}) が {int(VOICEVOX_START_TIMEOUT_S)} s 以内に応答しませんでした",
                "logs/voicevox_doctor.log を確認してください。ポートが他で使われていないかも確認してください。",
            )
        if version != expected:
            return CheckResult(
                "tts",
                "fail",
                f"VOICEVOX ENGINE の版が {version} です ({expected} が必要)",
                f"third_party/voicevox_engine/{expected}/ を削除してセットアップを再実行してください: "
                f"{setup_command()}",
            )
        return _synth_result(local_url, version, expected, started=True)
    finally:
        stop_process(proc)


# --- 4. vlm ------------------------------------------------------------------


def make_grey_jpeg(size: int = 64) -> bytes:
    """問い合わせに使う灰色の JPEG。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (size, size), (128, 128, 128)).save(buf, format="JPEG")
    return buf.getvalue()


def vlm_min_version(versions: dict[str, str]) -> str:
    """``VLM_MIN_VERSION`` env が優先、無ければ versions.env の ``OLLAMA_MIN_VERSION``。"""
    env = os.getenv("VLM_MIN_VERSION", "").strip()
    if env:
        return env
    return versions.get("OLLAMA_MIN_VERSION", "0.17.1")


def make_ollama_backend(settings: Any, min_version: str, timeout_s: float) -> Any:
    """テストで ``transport`` を差し込めるよう生成を分ける。"""
    from openmovehub.services.vlm import OllamaBackend

    return OllamaBackend(
        settings.vlm_url,
        settings.vlm_model,
        timeout_s=timeout_s,
        keep_alive=settings.vlm_keep_alive,
        num_ctx=settings.vlm_num_ctx,
        temperature=settings.vlm_temperature,
        num_predict=settings.vlm_num_predict,
        min_version=min_version,
    )


async def _vlm_check_async(settings: Any, min_version: str, timeout_s: float) -> CheckResult:
    backend = make_ollama_backend(settings, min_version, timeout_s)
    model = settings.vlm_model
    try:
        probe = await backend.probe()
        version = probe.get("version")
        if not probe.get("ok"):
            message = str(probe.get("message") or "")
            if version is None:
                hint = "Ollama を起動してください (ollama serve)。未導入ならセットアップを再実行してください: " + (
                    setup_command()
                )
            elif not probe.get("model_present") and "older" not in message:
                hint = f"ollama pull {model}"
            else:
                hint = f"Ollama を {min_version} 以上に更新してください (セットアップを再実行: {setup_command()})。"
            return CheckResult("vlm", "fail", message or "ollama probe failed", hint)

        show = await backend._request("POST", "/api/show", json_body={"model": model})
        capabilities = show.get("capabilities") if isinstance(show, dict) else None
        if not isinstance(capabilities, list) or "vision" not in capabilities:
            return CheckResult(
                "vlm",
                "fail",
                f"{model}: 画像に対応していないモデルです (capabilities={capabilities})",
                "VLM_MODEL に vision 対応のモデル (例: qwen3.5:9b) を指定してください。",
            )

        result = await backend.chat(
            [make_grey_jpeg()],
            "",
            "この画像は何色ですか。一言で答えてください。",
            None,
            {"num_predict": 32},
        )
        text = result.text.strip()
        if not text:
            return CheckResult("vlm", "fail", f"{model}: 応答が空でした", "ollama のログを確認してください。")
        return CheckResult(
            "vlm",
            "ok",
            f"ollama {version}, {model} (vision), load {result.load_ms / 1000:.2f} s, "
            f"response {result.ms / 1000:.2f} s: {text[:40]!r}",
        )
    finally:
        await backend.aclose()


def check_vlm(settings: Any, versions: dict[str, str], skip: bool = False) -> CheckResult:
    """Ollama の版・モデル・vision capability・灰色画像での 1 回の問い合わせ。"""
    if skip:
        return CheckResult("vlm", "skip", "--skip-vlm")
    if settings.vlm_backend == "none":
        return CheckResult("vlm", "skip", "VLM_BACKEND=none")
    timeout_s = max(float(settings.vlm_timeout_s), VLM_MIN_TIMEOUT_S)
    try:
        return asyncio.run(_vlm_check_async(settings, vlm_min_version(versions), timeout_s))
    except Exception as e:
        return CheckResult(
            "vlm",
            "fail",
            f"{settings.vlm_model}: 問い合わせに失敗しました: {_error_text(e)}",
            f"Ollama の状態を確認してください (タイムアウト {timeout_s:.0f} s)。",
        )


# --- 5. firmware ---------------------------------------------------------------


def find_pio() -> str | None:
    """``pio`` を PATH、無ければ ``~/.local/bin`` (``uv tool install`` の既定) から探す。"""
    found = shutil.which("pio")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / ("pio.exe" if is_windows() else "pio")
    return str(candidate) if candidate.is_file() else None


def platformio_core_dir() -> Path:
    env = os.getenv("PLATFORMIO_CORE_DIR", "").strip()
    return Path(env) if env else Path.home() / ".platformio"


def check_firmware(skip: bool = False) -> CheckResult:
    """``pio --version`` が成功し、ESP32-S3 のプラットフォームとツールチェーンがある。"""
    if skip:
        return CheckResult("firmware", "skip", "--skip-firmware")
    hint = (
        f"セットアップを再実行してください: {setup_command()} "
        "(または uv tool install platformio と pio pkg install -d firmware)"
    )
    pio = find_pio()
    if pio is None:
        return CheckResult("firmware", "fail", "pio が見つかりません", hint)
    rc, out = run_command([pio, "--version"], timeout=60.0)
    if rc != 0:
        return CheckResult("firmware", "fail", f"pio --version failed: {out.strip()[:200]}", hint)
    core = platformio_core_dir()
    missing = [
        pattern
        for pattern in ("platforms/espressif32*", "packages/toolchain-xtensa-esp32s3*")
        if not any(core.glob(pattern))
    ]
    if missing:
        return CheckResult(
            "firmware",
            "fail",
            f"{out.strip()}; {core} に {', '.join(missing)} がありません",
            "pio pkg install -d firmware と pio pkg install -d firmware_remote を実行してください "
            f"(またはセットアップを再実行: {setup_command()})。",
        )
    return CheckResult("firmware", "ok", f"{out.strip()}, esp32s3 toolchain in {core}")


# --- 6. network ----------------------------------------------------------------


def port_bindable(port: int) -> bool:
    """``0.0.0.0:<port>`` に bind できるか (uvicorn と同じく Windows 以外は SO_REUSEADDR)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not is_windows():
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def hub_running(port: int) -> bool:
    """``http://127.0.0.1:<port>/status`` が JSON object を返すか。"""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/status", timeout=3.0)
        return resp.is_success and isinstance(resp.json(), dict)
    except Exception:
        return False


def powershell(command: str) -> tuple[int, str]:
    return run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], timeout=30.0)


def firewall_rule_exists() -> bool:
    rc, out = powershell(
        f"(Get-NetFirewallRule -DisplayName '{FIREWALL_RULE_NAME}' -ErrorAction SilentlyContinue "
        "| Measure-Object).Count"
    )
    if rc != 0:
        return False
    try:
        return int(out.strip().splitlines()[-1]) > 0
    except (ValueError, IndexError):
        return False


def public_network_profiles() -> list[str]:
    rc, out = powershell(
        "Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' } | "
        "Select-Object -ExpandProperty Name"
    )
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def check_network(settings: Any) -> CheckResult:
    """Hub のポートが空いている。Windows はファイアウォール規則とネットワークの種類も見る。"""
    port = int(settings.port)
    status: Status = "ok"
    details: list[str] = []
    hints: list[str] = []
    if port_bindable(port):
        details.append(f"port {port} free")
    elif hub_running(port):
        status = "warn"
        details.append(f"port {port}: Hub が既に起動中")
        hints.append("Hub が起動したままです (問題ありません。止めるには起動したウィンドウで Ctrl+C)。")
    else:
        status = "fail"
        details.append(f"port {port}: 他のプロセスが使用中")
        hints.append(f"ポート {port} を使っているプロセスを止めるか、.env の HUB_PORT を変更してください。")

    if is_windows():
        if firewall_rule_exists():
            details.append(f"firewall rule '{FIREWALL_RULE_NAME}' present")
        else:
            status = "fail"
            details.append(f"firewall rule '{FIREWALL_RULE_NAME}' missing")
            hints.append(f"セットアップを再実行してファイアウォール規則を作ってください: {setup_command()}")
        public = public_network_profiles()
        if public:
            if status == "ok":
                status = "warn"
            details.append(f"public network: {', '.join(public)}")
            hints.append(
                "パブリックネットワークではロボットから接続できない可能性があります。"
                "Windows の設定 > ネットワークとインターネット で、このネットワークをプライベートに変更してください。"
            )
    return CheckResult("network", status, "; ".join(details), " ".join(hints) or None)


# --- 実行・表示 ------------------------------------------------------------------


def _guarded(name: str, func: Callable[[], CheckResult]) -> CheckResult:
    """例外は fail にして他の検査を止めない。"""
    try:
        return func()
    except Exception as e:
        return CheckResult(name, "fail", f"検査中に例外: {_error_text(e)}", None)


def run_checks(skip_vlm: bool = False, skip_firmware: bool = False, settings: Any = None) -> list[CheckResult]:
    if settings is None:
        from openmovehub.config import settings as default_settings

        settings = default_settings
    versions = read_versions()
    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("python", check_python),
        ("stt", lambda: check_stt(settings)),
        ("tts", lambda: check_tts(settings, versions)),
        ("vlm", lambda: check_vlm(settings, versions, skip=skip_vlm)),
        ("firmware", lambda: check_firmware(skip=skip_firmware)),
        ("network", lambda: check_network(settings)),
    ]
    return [_guarded(name, func) for name, func in checks]


def format_table(results: list[CheckResult]) -> str:
    """項目・状態・詳細の表と、FAIL / WARN の対処。"""
    lines = [f"{'項目':<8}  {'状態':<4}  詳細", "-" * 72]
    for r in results:
        lines.append(f"{r.name:<10}  {r.status.upper():<6}  {r.detail}")
        if r.hint and r.status in ("fail", "warn"):
            lines.append(f"{'':<10}  {'':<6}  -> 対処: {r.hint}")
    return "\n".join(lines)


def prefetch_stt(settings: Any = None) -> int:
    """``STT_PLUGIN=whisper`` のときだけ ``STT_MODEL`` を取得する。"""
    if settings is None:
        from openmovehub.config import settings as default_settings

        settings = default_settings
    if settings.stt_plugin != "whisper":
        print(f"[skip] STT_PLUGIN={settings.stt_plugin}")
        return 0
    model = stt_model_name()
    print(f"[prefetch] whisper model {model} ...", flush=True)
    try:
        path = whisper_download(model)
    except Exception as e:
        print(f"[fail] whisper model {model}: {_error_text(e)}")
        return 1
    print(f"[ok] whisper model {model}: {path}")
    return 0


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m openmovehub.doctor", description="導入結果の検査")
    parser.add_argument("--json", action="store_true", help="結果を JSON だけで出す")
    parser.add_argument("--skip-vlm", action="store_true", help="VLM (Ollama) の検査を飛ばす")
    parser.add_argument("--skip-firmware", action="store_true", help="PlatformIO の検査を飛ばす")
    parser.add_argument("--prefetch-stt", action="store_true", help="検査せず Whisper モデルを取得する")
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    if args.prefetch_stt:
        return prefetch_stt()
    results = run_checks(skip_vlm=args.skip_vlm, skip_firmware=args.skip_firmware)
    ok = not any(r.status == "fail" for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "checks": [r.to_dict() for r in results]}, ensure_ascii=False))
    else:
        print(format_table(results))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
