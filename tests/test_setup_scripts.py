"""セットアップ / 起動 / 書き込みスクリプトの静的検査と dry-run の確認。

ネットワーク・sudo は使わない (dry-run は何も導入・変更しない)。
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SHELL_SCRIPTS = [
    ROOT / "setup_ubuntu.sh",
    ROOT / "start_ubuntu.sh",
    ROOT / "flash_firmware_ubuntu.sh",
    SCRIPTS / "setup_ubuntu_lib.sh",
]
VERSIONS = SCRIPTS / "versions.env"


def _read_versions() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in VERSIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def _dry_run(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({"STT_PLUGIN": "whisper", "TTS_PLUGIN": "voicevox", "VLM_BACKEND": "ollama"})
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(ROOT / "setup_ubuntu.sh"), "--dry-run", "--yes", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _step_block(output: str, step: int) -> str:
    start = output.index(f"[{step}/11]")
    end = output.find(f"[{step + 1}/11]", start)
    return output[start:] if end < 0 else output[start:end]


def _git_status() -> str | None:
    if shutil.which("git") is None:
        return None
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--ignored"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    return result.stdout


# ---------------------------------------------------------------- 構文


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script: Path):
    assert script.is_file(), script
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SHELL_SCRIPTS[:3], ids=lambda p: p.name)
def test_entry_scripts_are_executable(script: Path):
    assert os.access(script, os.X_OK), f"{script.name} に実行権限がありません"


def test_shellcheck():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck がありません")
    result = subprocess.run(
        ["shellcheck", "-x", *[str(p.relative_to(ROOT)) for p in SHELL_SCRIPTS]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_powershell_syntax():
    ps1_files = sorted(SCRIPTS.glob("*.ps1"))
    if not ps1_files:
        pytest.skip("scripts/*.ps1 がありません")
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh がありません")
    for ps1 in ps1_files:
        command = (
            "$errors = $null; $tokens = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1}', [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Output $_.ToString() }; exit 1 }"
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"{ps1.name}: {result.stdout}{result.stderr}"


# ---------------------------------------------------------------- dry-run


def test_dry_run_shows_all_steps_and_voicevox():
    versions = _read_versions()
    before = _git_status()
    watched = [ROOT / ".env", ROOT / "logs", ROOT / "third_party"]
    existed = [p.exists() for p in watched]

    result = _dry_run()

    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    for step in range(1, 12):
        assert f"[{step}/11]" in out, f"手順 {step} の見出しがありません"
    assert versions["VOICEVOX_VERSION"] in out
    assert "VOICEVOX_LINUX_CPU_URL_1" in out
    assert versions["VOICEVOX_LINUX_CPU_URL_1"] in out
    assert "[dry-run]" in out

    # 何も変更しない (logs/ third_party/ .env も作らない)
    assert [p.exists() for p in watched] == existed
    after = _git_status()
    if before is not None:
        assert after == before


def test_dry_run_skip_vlm_skips_ollama():
    result = _dry_run("--skip-vlm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[skip]" in _step_block(result.stdout, 8)
    assert "ollama pull" not in result.stdout


def test_dry_run_vlm_backend_none_skips_ollama():
    result = _dry_run(extra_env={"VLM_BACKEND": "none"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[skip]" in _step_block(result.stdout, 8)


def test_dry_run_voicevox_gpu_shows_gpu_urls():
    versions = _read_versions()
    result = _dry_run("--voicevox-gpu")
    assert result.returncode == 0, result.stdout + result.stderr
    parts = int(versions["VOICEVOX_LINUX_GPU_PARTS"])
    for i in range(1, parts + 1):
        assert versions[f"VOICEVOX_LINUX_GPU_URL_{i}"] in result.stdout
    assert versions["VOICEVOX_LINUX_CPU_URL_1"] not in result.stdout


def test_dry_run_tts_none_skips_voicevox():
    result = _dry_run(extra_env={"TTS_PLUGIN": "none"})
    assert result.returncode == 0, result.stdout + result.stderr
    block = _step_block(result.stdout, 7)
    assert "[skip]" in block
    assert "curl" not in block


def test_dry_run_skip_firmware():
    result = _dry_run("--skip-firmware")
    assert result.returncode == 0, result.stdout + result.stderr
    block = _step_block(result.stdout, 9)
    assert "[skip]" in block
    assert "pio" not in block


@pytest.mark.parametrize("args", [["--no-such-option"], ["--vlm-model"]])
def test_bad_option_exits_2(args: list[str]):
    result = subprocess.run(
        ["bash", str(ROOT / "setup_ubuntu.sh"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "使い方" in result.stdout + result.stderr


def test_help_exits_0():
    result = subprocess.run(
        ["bash", str(ROOT / "setup_ubuntu.sh"), "--help"], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "--dry-run" in result.stdout


# ---------------------------------------------------------------- versions.env


def test_versions_env_urls_and_hashes():
    versions = _read_versions()
    urls = {k: v for k, v in versions.items() if "_URL" in k}
    hashes = {k: v for k, v in versions.items() if "SHA256" in k}
    assert urls and hashes
    for key, value in urls.items():
        assert value.startswith("https://"), key
    for key, value in hashes.items():
        assert re.fullmatch(r"[0-9a-f]{64}", value), key


def test_versions_env_parts_match():
    versions = _read_versions()
    prefixes = [k.removesuffix("_PARTS") for k in versions if k.endswith("_PARTS")]
    assert prefixes
    for prefix in prefixes:
        parts = int(versions[f"{prefix}_PARTS"])
        url_keys = [k for k in versions if re.fullmatch(rf"{prefix}_URL_\d+", k)]
        sha_keys = [k for k in versions if re.fullmatch(rf"{prefix}_SHA256_\d+", k)]
        assert len(url_keys) == parts, prefix
        assert len(sha_keys) == parts, prefix
        for i in range(1, parts + 1):
            assert f"{prefix}_URL_{i}" in versions
            assert f"{prefix}_SHA256_{i}" in versions


def test_versions_env_is_plain_key_value():
    # bash の source と PowerShell・Python の行パースの両方で読めるよう、引用符・空白・変数展開を使わない
    for line in VERSIONS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert re.fullmatch(r"[A-Z0-9_]+=[^\s\"'$`]*", line), line


# ---------------------------------------------------------------- Windows スクリプトの文字コード


def test_bat_files_crlf_ascii():
    bats = sorted(ROOT.glob("*.bat")) + sorted(SCRIPTS.glob("*.bat"))
    if not bats:
        pytest.skip(".bat がありません")
    for bat in bats:
        data = bat.read_bytes()
        assert data.isascii(), f"{bat.name} に ASCII 以外の文字があります"
        assert b"\n" in data
        assert data.count(b"\n") == data.count(b"\r\n"), f"{bat.name} の改行が CRLF ではありません"


def test_ps1_files_utf8_bom():
    ps1_files = sorted(SCRIPTS.glob("*.ps1"))
    if not ps1_files:
        pytest.skip("scripts/*.ps1 がありません")
    for ps1 in ps1_files:
        data = ps1.read_bytes()
        assert data.startswith(b"\xef\xbb\xbf"), f"{ps1.name} に UTF-8 BOM がありません"
        data.decode("utf-8")
