"""``openmovehub.doctor`` の検査 (外部呼び出しはすべてモック)。"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from openmovehub import doctor
from openmovehub.config import Settings
from openmovehub.doctor import CheckResult
from openmovehub.services.vlm import OllamaBackend

VERSIONS = {"VOICEVOX_VERSION": "0.25.2", "OLLAMA_MIN_VERSION": "0.17.1", "VOICEVOX_PORT": "50021"}


def make_settings(**kwargs) -> Settings:
    return Settings(**kwargs)


# --- versions.env ----------------------------------------------------------------


def test_read_versions_parses_key_value(tmp_path):
    path = tmp_path / "versions.env"
    path.write_text(
        "# comment\n\nVOICEVOX_VERSION=0.25.2\nOLLAMA_MIN_VERSION = 0.17.1\nbroken line\nURL=https://a/b?x=1\n"
    )
    values = doctor.read_versions(path)
    assert values == {"VOICEVOX_VERSION": "0.25.2", "OLLAMA_MIN_VERSION": "0.17.1", "URL": "https://a/b?x=1"}


def test_read_versions_missing_file(tmp_path):
    assert doctor.read_versions(tmp_path / "nope.env") == {}


def test_read_versions_repo_file():
    values = doctor.read_versions()
    assert values["VOICEVOX_VERSION"]
    assert values["OLLAMA_MIN_VERSION"]
    assert values["VOICEVOX_PORT"] == "50021"


def test_vlm_min_version_env_precedence(monkeypatch):
    monkeypatch.delenv("VLM_MIN_VERSION", raising=False)
    assert doctor.vlm_min_version(VERSIONS) == "0.17.1"
    monkeypatch.setenv("VLM_MIN_VERSION", "0.20.0")
    assert doctor.vlm_min_version(VERSIONS) == "0.20.0"


# --- python ------------------------------------------------------------------------


def test_check_python_ok():
    assert doctor.check_python().status == "ok"


# --- stt ---------------------------------------------------------------------------


def test_stt_none_skip():
    assert doctor.check_stt(make_settings(stt_plugin="none")).status == "skip"


def test_stt_unknown_plugin_fail():
    result = doctor.check_stt(make_settings(stt_plugin="no_such_plugin"))
    assert result.status == "fail"
    assert result.hint


def test_stt_third_party_ok(monkeypatch):
    monkeypatch.setattr(doctor, "resolve_plugin", lambda kind, name: object())
    assert doctor.check_stt(make_settings(stt_plugin="mystt")).status == "ok"


@pytest.fixture
def whisper_resolved(monkeypatch):
    monkeypatch.setattr(doctor, "resolve_plugin", lambda kind, name: object())
    monkeypatch.setenv("STT_MODEL", "tiny")


def test_stt_whisper_not_cached_fail(monkeypatch, whisper_resolved):
    def missing(model):
        raise FileNotFoundError(model)

    monkeypatch.setattr(doctor, "whisper_model_cached", missing)
    result = doctor.check_stt(make_settings(stt_plugin="whisper"))
    assert result.status == "fail"
    assert "--prefetch-stt" in result.hint


def test_stt_whisper_cpu_ok_without_load(monkeypatch, whisper_resolved):
    monkeypatch.setattr(doctor, "whisper_model_cached", lambda model: "/cache/tiny")
    monkeypatch.setattr(doctor, "cuda_device_count", lambda: 0)

    def no_load(model):
        raise AssertionError("must not load without GPU")

    monkeypatch.setattr(doctor, "whisper_cuda_trial", no_load)
    result = doctor.check_stt(make_settings(stt_plugin="whisper"))
    assert result.status == "ok"
    assert "cpu" in result.detail


def test_stt_whisper_cuda_ok(monkeypatch, whisper_resolved):
    monkeypatch.setattr(doctor, "whisper_model_cached", lambda model: "/cache/tiny")
    monkeypatch.setattr(doctor, "cuda_device_count", lambda: 1)
    monkeypatch.setattr(doctor, "whisper_cuda_trial", lambda model: None)
    result = doctor.check_stt(make_settings(stt_plugin="whisper"))
    assert result.status == "ok"
    assert "CUDA" in result.detail


@pytest.mark.parametrize("platform,ld_hint", [("linux", True), ("win32", False)])
def test_stt_whisper_cuda_fallback_warn(monkeypatch, whisper_resolved, platform, ld_hint):
    monkeypatch.setattr(doctor.sys, "platform", platform)
    monkeypatch.setattr(doctor, "whisper_model_cached", lambda model: "/cache/tiny")
    monkeypatch.setattr(doctor, "cuda_device_count", lambda: 1)
    monkeypatch.setattr(doctor, "whisper_cuda_trial", lambda model: "RuntimeError: libcublas.so.12 not found")
    result = doctor.check_stt(make_settings(stt_plugin="whisper"))
    assert result.status == "warn"
    assert "uv sync" in result.hint and "--group cuda" in result.hint
    assert ("LD_LIBRARY_PATH" in result.hint) is ld_hint


def test_whisper_cuda_trial_unloads(monkeypatch):
    calls = []

    class FakeStt:
        def __init__(self, model, device, compute_type):
            calls.append(("init", model, device, compute_type))
            self.fallback = "x"

        def load(self):
            calls.append("load")

        def unload(self):
            calls.append("unload")

    import openmovehub.plugins.stt_whisper as stt_whisper

    monkeypatch.setattr(stt_whisper, "WhisperStt", FakeStt)
    assert doctor.whisper_cuda_trial("tiny") == "x"
    assert calls == [("init", "tiny", "cuda", "auto"), "load", "unload"]


def test_whisper_model_cached_local_path(tmp_path):
    assert doctor.whisper_model_cached(str(tmp_path)) == str(tmp_path)


# --- tts ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_tts_none_skip():
    assert doctor.check_tts(make_settings(tts_plugin="none"), VERSIONS).status == "skip"


def test_tts_third_party(monkeypatch):
    assert doctor.check_tts(make_settings(tts_plugin="no_such"), VERSIONS).status == "fail"
    monkeypatch.setattr(doctor, "resolve_plugin", lambda kind, name: object())
    assert doctor.check_tts(make_settings(tts_plugin="mytts"), VERSIONS).status == "ok"


def test_tts_running_ok(monkeypatch):
    monkeypatch.setattr(doctor, "voicevox_version", lambda url, timeout=3.0: "0.25.2")
    monkeypatch.setattr(doctor, "voicevox_synthesize", lambda url, text=doctor.TTS_TEST_TEXT: 1000)
    monkeypatch.setattr(doctor, "start_voicevox", lambda exe, port: pytest.fail("must not start"))
    result = doctor.check_tts(make_settings(), VERSIONS)
    assert result.status == "ok"
    assert "0.25.2" in result.detail


def test_tts_running_version_mismatch_warn(monkeypatch):
    monkeypatch.setattr(doctor, "voicevox_version", lambda url, timeout=3.0: "0.24.0")
    monkeypatch.setattr(doctor, "voicevox_synthesize", lambda url, text=doctor.TTS_TEST_TEXT: 1000)
    assert doctor.check_tts(make_settings(), VERSIONS).status == "warn"


def test_tts_running_empty_pcm_fail(monkeypatch):
    monkeypatch.setattr(doctor, "voicevox_version", lambda url, timeout=3.0: "0.25.2")
    monkeypatch.setattr(doctor, "voicevox_synthesize", lambda url, text=doctor.TTS_TEST_TEXT: 0)
    assert doctor.check_tts(make_settings(), VERSIONS).status == "fail"


def test_tts_not_running_no_executable_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "voicevox_version", lambda url, timeout=3.0: None)
    monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path)
    result = doctor.check_tts(make_settings(), VERSIONS)
    assert result.status == "fail"
    assert "setup" in result.hint


@pytest.mark.parametrize("platform,exe_name", [("linux", "run"), ("win32", "run.exe")])
def test_voicevox_executable_per_os(monkeypatch, tmp_path, platform, exe_name):
    monkeypatch.setattr(doctor.sys, "platform", platform)
    monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path)
    assert doctor.voicevox_executable("0.25.2") == tmp_path / "third_party" / "voicevox_engine" / "0.25.2" / exe_name


def _install_fake_engine(monkeypatch, tmp_path, started_version):
    monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path)
    exe = doctor.voicevox_executable("0.25.2")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("VOICEVOX_URL", "http://127.0.0.1:50123")
    state = {"started": None, "proc": FakeProc()}

    def start(exe_path, port):
        state["started"] = (exe_path, port)
        return state["proc"]

    def version(url, timeout=3.0):
        return started_version if state["started"] else None

    monkeypatch.setattr(doctor, "start_voicevox", start)
    monkeypatch.setattr(doctor, "voicevox_version", version)
    monkeypatch.setattr(doctor, "voicevox_synthesize", lambda url, text=doctor.TTS_TEST_TEXT: 2000)
    return exe, state


def test_tts_temporary_start_ok_and_stopped(monkeypatch, tmp_path):
    exe, state = _install_fake_engine(monkeypatch, tmp_path, "0.25.2")
    result = doctor.check_tts(make_settings(), VERSIONS)
    assert result.status == "ok"
    assert state["started"] == (exe, 50123)
    assert state["proc"].terminated


def test_tts_temporary_start_version_mismatch_fail(monkeypatch, tmp_path):
    _, state = _install_fake_engine(monkeypatch, tmp_path, "0.24.0")
    assert doctor.check_tts(make_settings(), VERSIONS).status == "fail"
    assert state["proc"].terminated


def test_tts_temporary_start_timeout_fail_and_stopped(monkeypatch, tmp_path):
    _, state = _install_fake_engine(monkeypatch, tmp_path, None)
    monkeypatch.setattr(doctor, "wait_voicevox", lambda url, proc, timeout=60.0: None)
    assert doctor.check_tts(make_settings(), VERSIONS).status == "fail"
    assert state["proc"].terminated


def test_tts_temporary_start_stopped_on_exception(monkeypatch, tmp_path):
    _, state = _install_fake_engine(monkeypatch, tmp_path, "0.25.2")

    def boom(url, text=doctor.TTS_TEST_TEXT):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "voicevox_synthesize", boom)
    assert doctor.check_tts(make_settings(), VERSIONS).status == "fail"
    assert state["proc"].terminated


def test_stop_process_kill_fallback():
    class Stubborn(FakeProc):
        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("run", timeout)
            return self.returncode

    proc = Stubborn()
    doctor.stop_process(proc)
    assert proc.terminated and proc.killed


def test_voicevox_synthesize_with_mock_transport(monkeypatch):
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x01\x00" * 100)
    wav = buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/audio_query":
            return httpx.Response(200, json={"speedScale": 1.0})
        return httpx.Response(200, content=wav)

    import openmovehub.plugins.tts_voicevox as tts_voicevox

    original = tts_voicevox.VoicevoxTts.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, *args, **kwargs)

    monkeypatch.setattr(tts_voicevox.VoicevoxTts, "__init__", init)
    assert doctor.voicevox_synthesize("http://127.0.0.1:50021") == 200


# --- vlm ---------------------------------------------------------------------------


def ollama_handler(
    version="0.33.1",
    models=("qwen3.5:9b",),
    capabilities=("completion", "vision"),
    reply="灰色",
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if path == "/api/show":
            assert json.loads(request.content)["model"] == "qwen3.5:9b"
            return httpx.Response(200, json={"capabilities": list(capabilities)})
        if path == "/api/chat":
            body = json.loads(request.content)
            assert body["messages"][-1]["images"]
            return httpx.Response(
                200,
                json={"model": "qwen3.5:9b", "message": {"content": reply}, "load_duration": 2_500_000_000},
            )
        return httpx.Response(404)

    return handler


def use_ollama(monkeypatch, handler):
    def factory(settings, min_version, timeout_s):
        return OllamaBackend(
            settings.vlm_url,
            settings.vlm_model,
            timeout_s=timeout_s,
            min_version=min_version,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(doctor, "make_ollama_backend", factory)


def test_vlm_skip_flag_and_backend_none():
    assert doctor.check_vlm(make_settings(), VERSIONS, skip=True).status == "skip"
    assert doctor.check_vlm(make_settings(vlm_backend="none"), VERSIONS).status == "skip"


def test_vlm_ok(monkeypatch):
    monkeypatch.delenv("VLM_MIN_VERSION", raising=False)
    use_ollama(monkeypatch, ollama_handler())
    result = doctor.check_vlm(make_settings(), VERSIONS)
    assert result.status == "ok", result
    assert "load 2.50 s" in result.detail
    assert "response" in result.detail


def test_vlm_old_version_fail(monkeypatch):
    monkeypatch.delenv("VLM_MIN_VERSION", raising=False)
    use_ollama(monkeypatch, ollama_handler(version="0.10.0"))
    result = doctor.check_vlm(make_settings(), VERSIONS)
    assert result.status == "fail"
    assert "0.17.1" in result.hint


def test_vlm_model_missing_fail(monkeypatch):
    use_ollama(monkeypatch, ollama_handler(models=("other:1b",)))
    result = doctor.check_vlm(make_settings(), VERSIONS)
    assert result.status == "fail"
    assert "ollama pull qwen3.5:9b" in result.hint


def test_vlm_not_vision_fail(monkeypatch):
    use_ollama(monkeypatch, ollama_handler(capabilities=("completion",)))
    result = doctor.check_vlm(make_settings(), VERSIONS)
    assert result.status == "fail"
    assert "画像に対応していないモデルです" in result.detail


def test_vlm_empty_reply_fail(monkeypatch):
    use_ollama(monkeypatch, ollama_handler(reply="  "))
    assert doctor.check_vlm(make_settings(), VERSIONS).status == "fail"


def test_vlm_unreachable_fail(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    use_ollama(monkeypatch, handler)
    result = doctor.check_vlm(make_settings(), VERSIONS)
    assert result.status == "fail"
    assert "ollama serve" in result.hint


def test_vlm_timeout_at_least_120(monkeypatch):
    seen = {}

    def factory(settings, min_version, timeout_s):
        seen["timeout"] = timeout_s
        raise RuntimeError("stop")

    monkeypatch.setattr(doctor, "make_ollama_backend", factory)
    assert doctor.check_vlm(make_settings(vlm_timeout_s=60), VERSIONS).status == "fail"
    assert seen["timeout"] == 120.0


def test_grey_jpeg():
    data = doctor.make_grey_jpeg()
    assert data[:2] == b"\xff\xd8"


# --- firmware ------------------------------------------------------------------------


def test_firmware_skip():
    assert doctor.check_firmware(skip=True).status == "skip"


def test_firmware_pio_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: tmp_path))
    result = doctor.check_firmware()
    assert result.status == "fail"
    assert result.hint


@pytest.mark.parametrize("platform,exe_name", [("linux", "pio"), ("win32", "pio.exe")])
def test_find_pio_local_bin(monkeypatch, tmp_path, platform, exe_name):
    monkeypatch.setattr(doctor.sys, "platform", platform)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: tmp_path))
    pio = tmp_path / ".local" / "bin" / exe_name
    pio.parent.mkdir(parents=True)
    pio.write_text("")
    assert doctor.find_pio() == str(pio)


def _fake_core(tmp_path, platform=True, toolchain=True):
    core = tmp_path / "pio_core"
    if platform:
        (core / "platforms" / "espressif32").mkdir(parents=True)
    if toolchain:
        (core / "packages" / "toolchain-xtensa-esp32s3").mkdir(parents=True)
    return core


def test_firmware_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "find_pio", lambda: "/usr/bin/pio")
    monkeypatch.setattr(doctor, "run_command", lambda cmd, timeout=60.0: (0, "PlatformIO Core, version 6.1.18\n"))
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(_fake_core(tmp_path)))
    result = doctor.check_firmware()
    assert result.status == "ok"
    assert "6.1.18" in result.detail


def test_firmware_toolchain_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "find_pio", lambda: "/usr/bin/pio")
    monkeypatch.setattr(doctor, "run_command", lambda cmd, timeout=60.0: (0, "PlatformIO Core, version 6.1.18"))
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(_fake_core(tmp_path, toolchain=False)))
    result = doctor.check_firmware()
    assert result.status == "fail"
    assert "toolchain-xtensa-esp32s3" in result.detail


def test_firmware_pio_version_fails(monkeypatch):
    monkeypatch.setattr(doctor, "find_pio", lambda: "/usr/bin/pio")
    monkeypatch.setattr(doctor, "run_command", lambda cmd, timeout=60.0: (1, "broken"))
    assert doctor.check_firmware().status == "fail"


# --- network -------------------------------------------------------------------------


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")


def test_network_port_free_ok(monkeypatch, linux):
    monkeypatch.setattr(doctor, "port_bindable", lambda port: True)
    assert doctor.check_network(make_settings(port=8341)).status == "ok"


def test_network_hub_running_warn(monkeypatch, linux):
    monkeypatch.setattr(doctor, "port_bindable", lambda port: False)
    monkeypatch.setattr(doctor, "hub_running", lambda port: True)
    result = doctor.check_network(make_settings(port=8341))
    assert result.status == "warn"
    assert "Hub が既に起動中" in result.detail


def test_network_port_used_fail(monkeypatch, linux):
    monkeypatch.setattr(doctor, "port_bindable", lambda port: False)
    monkeypatch.setattr(doctor, "hub_running", lambda port: False)
    result = doctor.check_network(make_settings(port=8341))
    assert result.status == "fail"
    assert "他のプロセスが使用中" in result.detail


def test_port_bindable_real_socket(linux):
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("0.0.0.0", 0))
    sock.listen(1)
    try:
        port = sock.getsockname()[1]
        assert doctor.port_bindable(port) is False
    finally:
        sock.close()


def test_hub_running_never_raises():
    # 使われていないポート (bind してすぐ閉じる) への接続は失敗して False。
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert doctor.hub_running(port) is False


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor, "port_bindable", lambda port: True)


def fake_powershell(rule_count="1", public=""):
    def run(command):
        if "Get-NetFirewallRule" in command:
            return 0, rule_count + "\n"
        if "Get-NetConnectionProfile" in command:
            return 0, public
        raise AssertionError(command)

    return run


def test_network_windows_ok(monkeypatch, windows):
    monkeypatch.setattr(doctor, "powershell", fake_powershell())
    assert doctor.check_network(make_settings()).status == "ok"


def test_network_windows_firewall_missing_fail(monkeypatch, windows):
    monkeypatch.setattr(doctor, "powershell", fake_powershell(rule_count="0"))
    result = doctor.check_network(make_settings())
    assert result.status == "fail"
    assert "setup_windows.bat" in result.hint


def test_network_windows_public_warn(monkeypatch, windows):
    monkeypatch.setattr(doctor, "powershell", fake_powershell(public="Wi-Fi 1\n"))
    result = doctor.check_network(make_settings())
    assert result.status == "warn"
    assert "プライベート" in result.hint


def test_network_windows_powershell_error(monkeypatch, windows):
    monkeypatch.setattr(doctor, "powershell", lambda command: (-1, "FileNotFoundError"))
    assert doctor.check_network(make_settings()).status == "fail"


def test_firewall_rule_exists_calls_powershell(monkeypatch):
    seen = []

    def run(cmd, timeout=60.0):
        seen.append(cmd)
        return 0, "1\r\n"

    monkeypatch.setattr(doctor, "run_command", run)
    assert doctor.firewall_rule_exists() is True
    assert seen[0][0] == "powershell"
    assert doctor.FIREWALL_RULE_NAME in seen[0][-1]


# --- main / 終了コード / JSON ---------------------------------------------------------------


def patch_checks(monkeypatch, statuses):
    names = ["python", "stt", "tts", "vlm", "firmware", "network"]
    results = {n: CheckResult(n, s, f"{n} detail", "hint" if s in ("fail", "warn") else None) for n, s in statuses}
    funcs = {
        "check_python": lambda: results.get("python", CheckResult("python", "ok", "")),
        "check_stt": lambda settings: results.get("stt", CheckResult("stt", "ok", "")),
        "check_tts": lambda settings, versions: results.get("tts", CheckResult("tts", "ok", "")),
        "check_vlm": lambda settings, versions, skip=False: results.get("vlm", CheckResult("vlm", "ok", "")),
        "check_firmware": lambda skip=False: results.get("firmware", CheckResult("firmware", "ok", "")),
        "check_network": lambda settings: results.get("network", CheckResult("network", "ok", "")),
    }
    for attr, func in funcs.items():
        monkeypatch.setattr(doctor, attr, func)
    return names


def test_main_all_ok_exit0(monkeypatch, capsys):
    patch_checks(monkeypatch, [])
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "SETUP OK" not in out


def test_main_fail_exit1_with_hint(monkeypatch, capsys):
    patch_checks(monkeypatch, [("tts", "fail")])
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "対処" in out


def test_main_warn_only_exit0(monkeypatch, capsys):
    patch_checks(monkeypatch, [("stt", "warn"), ("network", "warn")])
    assert doctor.main([]) == 0
    assert "WARN" in capsys.readouterr().out


def test_main_json_shape(monkeypatch, capsys):
    patch_checks(monkeypatch, [("vlm", "fail"), ("firmware", "skip")])
    assert doctor.main(["--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert [c["name"] for c in data["checks"]] == ["python", "stt", "tts", "vlm", "firmware", "network"]
    for check in data["checks"]:
        assert set(check) == {"name", "status", "detail", "hint"}
        assert check["status"] in ("ok", "warn", "fail", "skip")
    by_name = {c["name"]: c for c in data["checks"]}
    assert by_name["vlm"]["hint"] == "hint"
    assert by_name["python"]["hint"] is None


def test_main_json_ok(monkeypatch, capsys):
    patch_checks(monkeypatch, [("stt", "warn")])
    assert doctor.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_main_passes_skip_flags(monkeypatch, capsys):
    seen = {}
    patch_checks(monkeypatch, [])

    def vlm(settings, versions, skip=False):
        seen["vlm"] = skip
        return CheckResult("vlm", "skip" if skip else "ok", "")

    def firmware(skip=False):
        seen["firmware"] = skip
        return CheckResult("firmware", "skip" if skip else "ok", "")

    monkeypatch.setattr(doctor, "check_vlm", vlm)
    monkeypatch.setattr(doctor, "check_firmware", firmware)
    assert doctor.main(["--json", "--skip-vlm", "--skip-firmware"]) == 0
    assert seen == {"vlm": True, "firmware": True}


def test_check_exception_becomes_fail(monkeypatch, capsys):
    patch_checks(monkeypatch, [])

    def boom(settings):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(doctor, "check_stt", boom)
    assert doctor.main(["--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    stt = next(c for c in data["checks"] if c["name"] == "stt")
    assert stt["status"] == "fail" and "unexpected" in stt["detail"]
    # 他の検査は続行される。
    assert len(data["checks"]) == 6


# --- --prefetch-stt ----------------------------------------------------------------------


def test_prefetch_skip_non_whisper(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "whisper_download", lambda model: pytest.fail("must not download"))
    assert doctor.prefetch_stt(SimpleNamespace(stt_plugin="none")) == 0
    assert "[skip] STT_PLUGIN=none" in capsys.readouterr().out


def test_prefetch_success(monkeypatch, capsys):
    monkeypatch.setenv("STT_MODEL", "tiny")
    seen = []
    monkeypatch.setattr(doctor, "whisper_download", lambda model: seen.append(model) or "/cache/tiny")
    assert doctor.prefetch_stt(SimpleNamespace(stt_plugin="whisper")) == 0
    assert seen == ["tiny"]
    assert "/cache/tiny" in capsys.readouterr().out


def test_prefetch_failure(monkeypatch, capsys):
    def fail(model):
        raise OSError("network down")

    monkeypatch.setattr(doctor, "whisper_download", fail)
    assert doctor.prefetch_stt(SimpleNamespace(stt_plugin="whisper")) == 1
    assert "[fail]" in capsys.readouterr().out


def test_prefetch_local_path(tmp_path):
    assert doctor.whisper_download(str(tmp_path)) == str(tmp_path)


def test_main_prefetch_does_not_run_checks(monkeypatch):
    monkeypatch.setattr(doctor, "run_checks", lambda **kw: pytest.fail("checks must not run"))
    monkeypatch.setattr(doctor, "prefetch_stt", lambda: 0)
    assert doctor.main(["--prefetch-stt"]) == 0


def test_repo_root():
    assert (doctor.REPO_ROOT / "pyproject.toml").is_file()
    assert doctor.VERSIONS_ENV == doctor.REPO_ROOT / "scripts" / "versions.env"
    assert isinstance(doctor.REPO_ROOT, Path)
