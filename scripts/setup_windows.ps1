# Windows 10 (22H2) / 11 (x64) 向け 1 クリックセットアップ。入口は setup_windows.bat。
# 文字コード: UTF-8 (BOM 付き)・CRLF。Windows PowerShell 5.1 と PowerShell 7 の両方で動くこと。
# 使い方: setup_windows.bat [--check] [--dry-run] [--skip-vlm] [--skip-firmware] [--vlm-model TAG] [--voicevox-gpu] [--yes]

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common_windows.ps1')
Initialize-Console

$TOTAL = 11
$FirewallRuleName = 'Open StackChan Move Hub'
$FtdiUrl = 'https://ftdichip.com/drivers/vcp-drivers/'

function Show-Usage {
    Write-Host @'
使い方: setup_windows.bat [オプション]

  --check           導入せず検査だけを行う (手順 1 と 11)
  --dry-run         実行する予定の手順とコマンドを表示するだけ (何も変更しない)
  --skip-vlm        VLM (Ollama とモデル) を導入しない
  --skip-firmware   PlatformIO (ファームの書き込み環境) を導入しない
  --vlm-model TAG   VLM のモデルを指定する (.env の VLM_MODEL にも書く)
  --voicevox-gpu    VOICEVOX ENGINE の GPU 版 (DirectML) を入れる
  --yes             確認を出さない
  -h, --help        この説明を表示する
'@
}

# ---- 引数 ($args を手でパース: --skip-vlm 形式のため param() は使わない) ----
$Check = $false; $DryRun = $false; $SkipVlm = $false; $SkipFirmware = $false
$VlmModelArg = $null; $VoicevoxGpu = $false; $Yes = $false
for ($i = 0; $i -lt $args.Count; $i++) {
    $a = [string]$args[$i]
    switch -CaseSensitive ($a) {
        '--check' { $Check = $true }
        '--dry-run' { $DryRun = $true }
        '--skip-vlm' { $SkipVlm = $true }
        '--skip-firmware' { $SkipFirmware = $true }
        '--voicevox-gpu' { $VoicevoxGpu = $true }
        '--yes' { $Yes = $true }
        '-y' { $Yes = $true }
        '--vlm-model' {
            $i++
            if ($i -ge $args.Count -or [string]::IsNullOrEmpty([string]$args[$i])) {
                Write-Host '--vlm-model にはモデル名を指定してください'
                Show-Usage
                exit 2
            }
            $VlmModelArg = [string]$args[$i]
        }
        '-h' { Show-Usage; exit 0 }
        '--help' { Show-Usage; exit 0 }
        default {
            if ($a.StartsWith('--vlm-model=')) {
                $VlmModelArg = $a.Substring('--vlm-model='.Length)
            } else {
                Write-Host "不明なオプション: $a"
                Show-Usage
                exit 2
            }
        }
    }
}

$RepoRoot = Get-RepoRoot
Set-Location -LiteralPath $RepoRoot
$V = Read-Versions $RepoRoot
if (-not $V.ContainsKey('VOICEVOX_VERSION')) {
    Write-Host "scripts\versions.env が読めません: $(Join-Path $RepoRoot 'scripts\versions.env')"
    exit 2
}
Add-ToolPaths

$script:ExitCode = 0
$script:HasGpu = $false
$script:ShowFtdiNote = $false

# ---- 補助関数 ----
function Write-Step([int]$N, [string]$Name) {
    Write-Host ''
    Write-Host "== [$N/$TOTAL] $Name =="
}

function Write-Skip([string]$Reason) { Write-Host "[skip] $Reason" }
function Write-Ok([string]$Msg) { Write-Host "[ok] $Msg" }
function Write-Warn([string]$Msg) { Write-Host "[warn] $Msg" -ForegroundColor Yellow }

function Stop-Setup([int]$Code, [string]$Msg) {
    # 理由を出して終了コード Code で終わる (Stop-Transcript は最上位の finally で行う)
    if ($Msg) { Write-Host "[error] $Msg" -ForegroundColor Red }
    $script:ExitCode = $Code
    throw 'SETUP_ABORT'
}

function Format-Cmd([string]$Exe, [string[]]$Arguments) {
    $parts = @($Exe) + @($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } })
    return ($parts -join ' ')
}

function Invoke-Native {
    # 外部コマンドを実行して終了コードを返す。dry-run では表示だけ。
    # -Capture: 出力を Write-Host に流してログにも残す (進捗表示のあるダウンロード系は付けない)
    param([string]$Exe, [string[]]$Arguments = @(), [switch]$Capture, [switch]$Always)
    if ($DryRun -and -not $Always) {
        Write-Host "[dry-run] $(Format-Cmd $Exe $Arguments)"
        return 0
    }
    Write-Host "> $(Format-Cmd $Exe $Arguments)"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Capture) {
            & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host ([string]$_) }
        } else {
            # stdout は Out-Host で画面へ (関数の戻り値に混ざらないように)。stderr はそのまま画面へ
            & $Exe @Arguments | Out-Host
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($null -eq $code) { $code = 0 }
    return [int]$code
}

function Invoke-NativeChecked {
    param([string]$Exe, [string[]]$Arguments = @(), [switch]$Capture, [string]$FailMsg, [int]$FailCode = 1)
    $code = Invoke-Native -Exe $Exe -Arguments $Arguments -Capture:$Capture
    if ($code -ne 0) {
        if (-not $FailMsg) { $FailMsg = "コマンドが失敗しました (終了コード $code): $(Format-Cmd $Exe $Arguments)" }
        Stop-Setup $FailCode $FailMsg
    }
}

function Get-NativeOutput {
    # 読み取り専用の外部コマンドの出力 (stdout+stderr) を文字列で返す。dry-run でも実行する
    param([string]$Exe, [string[]]$Arguments = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>&1 | ForEach-Object { [string]$_ }
        $script:LastNativeCode = $LASTEXITCODE
    } catch {
        $out = @()
        $script:LastNativeCode = 1
    } finally {
        $ErrorActionPreference = $prev
    }
    return (@($out) -join "`n")
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-FileSha256([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    return ((Get-FileSha256 $Path) -eq $Expected.ToLowerInvariant())
}

function Save-Download {
    # URL を Dest に取得する。curl.exe (Windows 10 1803+ 同梱) の -C - で途中から再開できる。
    # curl.exe が無い場合だけ BITS → Invoke-WebRequest にフォールバック (再開はできない)
    param([string]$Url, [string]$Dest)
    $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($curl) {
        $code = Invoke-Native -Exe $curl.Source -Arguments @('-fL', '-C', '-', '--retry', '5', '--retry-delay', '5', '-o', $Dest, $Url)
        if ($code -ne 0) {
            Stop-Setup 1 "ダウンロードに失敗しました (curl 終了コード $code): $Url`n  もう一度 setup_windows.bat を実行すると途中から再開します。"
        }
        return
    }
    if ($DryRun) { Write-Host "[dry-run] download $Url -> $Dest"; return }
    Write-Host "> download $Url -> $Dest (curl.exe が無いため途中再開はできません)"
    if (Test-Path -LiteralPath $Dest) { Remove-Item -LiteralPath $Dest -Force }
    if (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue) {
        try {
            Start-BitsTransfer -Source $Url -Destination $Dest -ErrorAction Stop
            return
        } catch {
            Write-Warn "BITS でのダウンロードに失敗しました。Invoke-WebRequest で再試行します: $($_.Exception.Message)"
        }
    }
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
    } catch {
        Stop-Setup 1 "ダウンロードに失敗しました: $Url ($($_.Exception.Message))"
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    # python-dotenv が読むので BOM 無しの UTF-8 で保存する
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

function Get-DoctorArgs {
    $a = @('run', 'python', '-m', 'openmovehub.doctor')
    if ($SkipVlm) { $a += '--skip-vlm' }
    if ($SkipFirmware) { $a += '--skip-firmware' }
    return $a
}

# ---- 手順 ----
function Step1-Precheck {
    Write-Step 1 '事前確認'
    $problems = @()
    if ([Environment]::Is64BitOperatingSystem) { Write-Ok 'x64' } else { $problems += 'x64 版の Windows が必要です' }

    $build = [Environment]::OSVersion.Version.Build
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        $problems += "Windows ではありません ($([Environment]::OSVersion.VersionString))"
    } elseif ($build -lt 19045) {
        $problems += "Windows 10 22H2 (ビルド 19045) 以降が必要です (現在 $build)"
    } else {
        Write-Ok "Windows ビルド $build"
    }

    $minGb = [int]$V['MIN_DISK_GB']
    if ($SkipVlm) { $minGb = [int]$V['MIN_DISK_GB_NO_VLM'] }
    try {
        $drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($RepoRoot))
        $freeGb = [math]::Floor($drive.AvailableFreeSpace / 1GB)
        if ($freeGb -lt $minGb) {
            $problems += "空きディスクが足りません ($($drive.Name): $freeGb GB、必要 $minGb GB 以上)"
        } else {
            Write-Ok "空きディスク $freeGb GB ($($drive.Name)、必要 $minGb GB)"
        }
    } catch {
        Write-Warn "空きディスクを確認できませんでした: $($_.Exception.Message)"
    }

    try {
        Invoke-WebRequest -Method Head -Uri 'https://github.com' -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop | Out-Null
        Write-Ok 'インターネット接続 (https://github.com)'
    } catch {
        $problems += 'インターネットに接続できません (https://github.com に届きません)'
    }

    $script:HasGpu = $false
    if (Test-CommandExists 'nvidia-smi') {
        Get-NativeOutput 'nvidia-smi' @('-L') | Out-Null
        if ($script:LastNativeCode -eq 0) { $script:HasGpu = $true }
    }
    if ($script:HasGpu) { Write-Ok 'NVIDIA GPU あり (CUDA ライブラリを入れます)' } else { Write-Host '[info] NVIDIA GPU なし (Whisper は CPU で動かします)' }

    if ($problems.Count -gt 0) {
        foreach ($p in $problems) { Write-Warn $p }
        if ($DryRun -or $Check) {
            Write-Warn '条件を満たしていませんが、--dry-run / --check のため続けます'
        } else {
            Stop-Setup 2 '事前確認で条件を満たしていません'
        }
    }
}

function Step2-OsPackages {
    Write-Step 2 'OS のパッケージ (winget・Visual C++ 再頒布可能パッケージ・7zr.exe)'
    if (-not (Test-CommandExists 'winget')) {
        $msg = 'winget が見つかりません。Microsoft Store で「アプリ インストーラー」を入れてから、もう一度実行してください。'
        if ($DryRun) { Write-Warn $msg } else { Stop-Setup 2 $msg }
    } else {
        $vcOk = $false
        try {
            $reg = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction Stop
            if ($reg.Installed -eq 1) { $vcOk = $true }
        } catch { }
        if (-not $vcOk) {
            Get-NativeOutput 'winget' @('list', '-e', '--id', 'Microsoft.VCRedist.2015+.x64', '--accept-source-agreements') | Out-Null
            if ($script:LastNativeCode -eq 0) { $vcOk = $true }
        }
        if ($vcOk) {
            Write-Skip 'Visual C++ 再頒布可能パッケージ (x64) は導入済み'
        } else {
            $code = Invoke-Native 'winget' @('install', '-e', '--id', 'Microsoft.VCRedist.2015+.x64', '--silent', '--accept-package-agreements', '--accept-source-agreements')
            # -1978335189: 適用できる更新なし / -1978335135: 導入済み
            if ($code -ne 0 -and $code -ne -1978335189 -and $code -ne -1978335135) {
                Stop-Setup 1 "Visual C++ 再頒布可能パッケージの導入に失敗しました (winget 終了コード $code)"
            }
        }
    }

    $toolsDir = Join-Path $RepoRoot 'third_party\tools'
    $sevenZr = Join-Path $toolsDir '7zr.exe'
    if (Test-FileSha256 $sevenZr $V['SEVENZR_SHA256']) {
        Write-Skip '7zr.exe は取得済み (SHA256 一致)'
        return
    }
    if ($DryRun) {
        Write-Host "[dry-run] download $($V['SEVENZR_URL']) -> $sevenZr (SHA256 $($V['SEVENZR_SHA256']))"
        return
    }
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    $tmp = "$sevenZr.download"
    Write-Host "> download $($V['SEVENZR_URL'])"
    try {
        Invoke-WebRequest -Uri $V['SEVENZR_URL'] -OutFile $tmp -UseBasicParsing
    } catch {
        Stop-Setup 1 "7zr.exe のダウンロードに失敗しました: $($_.Exception.Message)"
    }
    if (-not (Test-FileSha256 $tmp $V['SEVENZR_SHA256'])) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        Stop-Setup 1 "7zr.exe の SHA256 が一致しません (期待 $($V['SEVENZR_SHA256']))"
    }
    Move-Item -LiteralPath $tmp -Destination $sevenZr -Force
    Write-Ok '7zr.exe を取得しました'
}

function Step3-Uv {
    Write-Step 3 'uv'
    if (Test-CommandExists 'uv') {
        $ver = Get-NativeOutput 'uv' @('--version')
        Write-Skip "uv は導入済み ($ver)"
        return
    }
    Invoke-NativeChecked 'powershell' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-c', 'irm https://astral.sh/uv/install.ps1 | iex') -Capture -FailMsg 'uv の導入に失敗しました'
    if ($DryRun) { return }
    Add-ToolPaths
    if (-not (Test-CommandExists 'uv')) {
        Stop-Setup 1 "uv を導入しましたが見つかりません ($env:USERPROFILE\.local\bin)"
    }
    Write-Ok "uv を導入しました ($(Get-NativeOutput 'uv' @('--version')))"
}

function Step4-PythonDeps {
    Write-Step 4 'Python と依存 (uv sync)'
    $a = @('sync', '--frozen')
    if ($script:HasGpu) { $a += @('--group', 'cuda') }
    Invoke-NativeChecked 'uv' $a -FailMsg 'uv sync に失敗しました'
}

function Step5-ConfigFiles {
    Write-Step 5 '設定ファイル (.env・config.h)'
    $envPath = Join-Path $RepoRoot '.env'
    $examplePath = Join-Path $RepoRoot '.env.example'
    if (Test-Path -LiteralPath $envPath) {
        Write-Skip '.env は既にあります (変更しません)'
    } elseif ($DryRun) {
        $note = ''
        if (-not $script:HasGpu) { $note = ' (STT_DEVICE=cpu に書き換え)' }
        Write-Host "[dry-run] copy .env.example -> .env$note"
    } else {
        $text = [IO.File]::ReadAllText($examplePath)
        if (-not $script:HasGpu) {
            $text = [regex]::Replace($text, '(?m)^STT_DEVICE=[^\r\n]*', 'STT_DEVICE=cpu')
        }
        Write-Utf8NoBom $envPath $text
        Write-Ok '.env を .env.example から作りました'
    }

    if ($VlmModelArg) {
        if ($DryRun) {
            Write-Host "[dry-run] .env の VLM_MODEL を $VlmModelArg にする"
        } else {
            $text = [IO.File]::ReadAllText($envPath)
            if ($text -match '(?m)^VLM_MODEL=') {
                $text = [regex]::Replace($text, '(?m)^VLM_MODEL=[^\r\n]*', "VLM_MODEL=$VlmModelArg")
            } else {
                $nl = "`n"
                if ($text -match "`r`n") { $nl = "`r`n" }
                if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $text += $nl }
                $text += "VLM_MODEL=$VlmModelArg$nl"
            }
            Write-Utf8NoBom $envPath $text
            Write-Ok ".env の VLM_MODEL を $VlmModelArg にしました"
        }
    }

    foreach ($fw in @('firmware', 'firmware_remote')) {
        $cfgH = Join-Path $RepoRoot "$fw\include\config.h"
        $exH = Join-Path $RepoRoot "$fw\include\config.h.example"
        if (Test-Path -LiteralPath $cfgH) {
            Write-Skip "$fw\include\config.h は既にあります"
        } elseif ($DryRun) {
            Write-Host "[dry-run] copy $fw\include\config.h.example -> $fw\include\config.h"
        } else {
            Copy-Item -LiteralPath $exH -Destination $cfgH
            Write-Ok "$fw\include\config.h を作りました"
        }
    }
}

function Step6-Whisper {
    param([hashtable]$Cfg)
    Write-Step 6 'Whisper モデル'
    if ($Cfg['STT_PLUGIN'] -ne 'whisper') {
        Write-Skip "STT_PLUGIN=$($Cfg['STT_PLUGIN']) (whisper 以外)"
        return
    }
    Invoke-NativeChecked 'uv' @('run', 'python', '-m', 'openmovehub.doctor', '--prefetch-stt') -FailMsg 'Whisper モデルの取得に失敗しました'
}

function Step7-Voicevox {
    param([hashtable]$Cfg)
    Write-Step 7 'VOICEVOX ENGINE'
    if ($Cfg['TTS_PLUGIN'] -ne 'voicevox') {
        Write-Skip "TTS_PLUGIN=$($Cfg['TTS_PLUGIN']) (voicevox 以外)"
        return
    }
    $ver = $V['VOICEVOX_VERSION']
    $engineDir = Get-VoicevoxDir $RepoRoot $V
    $runExe = Join-Path $engineDir 'run.exe'
    if (Test-Path -LiteralPath $runExe) {
        Write-Skip "VOICEVOX ENGINE $ver は展開済み ($engineDir)"
        return
    }
    $kind = 'CPU'
    if ($VoicevoxGpu) { $kind = 'GPU' }
    $prefix = "VOICEVOX_WINDOWS_${kind}_"
    $parts = [int]$V["${prefix}PARTS"]
    Write-Host "版: $ver ($kind 版、$parts 分割)"
    for ($n = 1; $n -le $parts; $n++) { Write-Host "  URL: $($V["${prefix}URL_$n"])" }

    $sevenZr = Join-Path $RepoRoot 'third_party\tools\7zr.exe'
    $dlDir = Join-Path $RepoRoot 'third_party\downloads'
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $dlDir | Out-Null }

    $files = @()
    for ($n = 1; $n -le $parts; $n++) {
        $url = $V["${prefix}URL_$n"]
        $sha = $V["${prefix}SHA256_$n"]
        $dest = Join-Path $dlDir ([IO.Path]::GetFileName(([uri]$url).AbsolutePath))
        $files += $dest
        if (Test-FileSha256 $dest $sha) {
            Write-Skip "$([IO.Path]::GetFileName($dest)) は取得済み (SHA256 一致)"
            continue
        }
        Save-Download $url $dest
        if ($DryRun) { Write-Host "[dry-run] SHA256 を検査 ($sha)"; continue }
        Write-Host "SHA256 を検査しています: $([IO.Path]::GetFileName($dest))"
        if (-not (Test-FileSha256 $dest $sha)) {
            Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
            Stop-Setup 1 "SHA256 が一致しません ($([IO.Path]::GetFileName($dest))、期待 $sha)。ファイルを消しました。もう一度実行してください。"
        }
        Write-Ok 'SHA256 一致'
    }

    $archive = $files[0]
    if ($parts -gt 1) {
        # 7zr.exe は分割アーカイブを扱えない可能性があるため、部品をバイナリ結合して 1 つの .7z にする
        $archive = Join-Path $dlDir "voicevox_engine-$ver.7z"
        if ($DryRun) {
            Write-Host "[dry-run] $parts 個の部品を結合 -> $archive"
        } else {
            Write-Host "部品を結合しています -> $archive"
            $out = [IO.File]::Create($archive)
            try {
                foreach ($f in $files) {
                    $in = [IO.File]::OpenRead($f)
                    try { $in.CopyTo($out) } finally { $in.Dispose() }
                }
            } finally { $out.Dispose() }
        }
    }

    $tmpDir = Join-Path $RepoRoot 'third_party\voicevox_engine\_extract'
    if ($DryRun) {
        Write-Host "[dry-run] $(Format-Cmd $sevenZr @('x', '-t7z', $archive, "-o$tmpDir", '-y'))"
        Write-Host "[dry-run] run.exe を含むディレクトリ -> $engineDir"
        Write-Host "[dry-run] $runExe --help"
        Write-Host "[dry-run] remove $dlDir"
        return
    }
    if (-not (Test-Path -LiteralPath $sevenZr)) { Stop-Setup 1 "7zr.exe がありません ($sevenZr)。手順 2 を確認してください。" }
    if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    # .7z.001 の拡張子で分割形式と判定されないよう、形式 (-t7z) を明示する
    Invoke-NativeChecked $sevenZr @('x', '-t7z', $archive, "-o$tmpDir", '-y') -FailMsg 'VOICEVOX ENGINE の展開に失敗しました'
    $found = Get-ChildItem -LiteralPath $tmpDir -Recurse -Filter 'run.exe' -File | Select-Object -First 1
    if (-not $found) { Stop-Setup 1 '展開したファイルに run.exe が見つかりません' }
    if (Test-Path -LiteralPath $engineDir) { Remove-Item -LiteralPath $engineDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $engineDir) | Out-Null
    Move-Item -LiteralPath $found.DirectoryName -Destination $engineDir
    if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force }

    $code = Invoke-Native $runExe @('--help') -Capture
    if ($code -ne 0) { Stop-Setup 1 "VOICEVOX ENGINE の run.exe --help が失敗しました (終了コード $code)" }
    Remove-Item -LiteralPath $dlDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "VOICEVOX ENGINE $ver を $engineDir に展開しました"
}

function Get-OllamaVersion([string]$Exe) {
    $out = Get-NativeOutput $Exe @('--version')
    $m = [regex]::Match($out, '(\d+)\.(\d+)\.(\d+)')
    if ($m.Success) { return [version]$m.Value }
    return $null
}

function Step8-Ollama {
    param([hashtable]$Cfg, [string]$Model)
    Write-Step 8 'Ollama と VLM モデル'
    if ($SkipVlm) { Write-Skip '--skip-vlm'; return }
    if ($Cfg['VLM_BACKEND'] -eq 'none') { Write-Skip 'VLM_BACKEND=none'; return }

    $minVer = [version]$V['OLLAMA_MIN_VERSION']
    $wingetArgs = @('-e', '--id', 'Ollama.Ollama', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    $exe = Get-OllamaExe
    if (-not $exe) {
        $code = Invoke-Native 'winget' (@('install') + $wingetArgs)
        if ($code -ne 0 -and $code -ne -1978335135) { Stop-Setup 1 "Ollama の導入に失敗しました (winget 終了コード $code)" }
    } else {
        $cur = Get-OllamaVersion $exe
        if ($null -eq $cur -or $cur -lt $minVer) {
            Write-Host "Ollama $cur は $minVer 未満のため更新します"
            $code = Invoke-Native 'winget' (@('upgrade') + $wingetArgs)
            if ($code -ne 0 -and $code -ne -1978335189) { Stop-Setup 1 "Ollama の更新に失敗しました (winget 終了コード $code)" }
        } else {
            Write-Skip "Ollama $cur は導入済み (最小 $minVer)"
        }
    }
    Add-ToolPaths

    $apiUrl = "$($Cfg['VLM_URL'])/api/version"
    if ($DryRun) {
        Write-Host "[dry-run] $apiUrl の応答を最大 60 s 待つ (応答しなければ ollama serve を起動)"
        Write-Host "[dry-run] ollama list に $Model が無ければ: ollama pull $Model"
        Write-Host "[dry-run] ollama show $Model で vision を確認"
        return
    }
    $exe = Get-OllamaExe
    if (-not $exe) { Stop-Setup 1 'ollama が見つかりません。Ollama を手動で入れてから、もう一度実行してください。' }
    $cur = Get-OllamaVersion $exe
    if ($null -ne $cur -and $cur -lt $minVer) { Stop-Setup 1 "Ollama $cur は $minVer 未満です" }

    Write-Host "Ollama の応答を待っています ($apiUrl、最大 60 s)"
    if (-not (Wait-HttpOk $apiUrl 60)) {
        Write-Host 'ollama serve を起動します'
        Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
        if (-not (Wait-HttpOk $apiUrl 60)) { Stop-Setup 1 "Ollama が応答しません ($apiUrl)" }
    }
    Write-Ok 'Ollama が応答しました'

    $list = Get-NativeOutput $exe @('list')
    $names = @($list -split "`n" | Select-Object -Skip 1 | ForEach-Object { ($_.Trim() -split '\s+')[0] } | Where-Object { $_ })
    $want = @($Model)
    if ($Model -notmatch ':') { $want += "${Model}:latest" }
    $have = $false
    foreach ($n in $names) { if ($want -contains $n) { $have = $true } }
    if ($have) {
        Write-Skip "モデル $Model は取得済み"
    } else {
        Invoke-NativeChecked $exe @('pull', $Model) -FailMsg "モデル $Model の取得に失敗しました"
    }

    $show = Get-NativeOutput $exe @('show', $Model)
    if ($show -notmatch '(?m)^\s*vision\s*$') {
        Stop-Setup 2 "画像に対応していないモデルです ($Model)。vision 対応のモデルを --vlm-model で指定してください。"
    }
    Write-Ok "モデル $Model は vision に対応しています"
}

function Step9-PlatformIO {
    Write-Step 9 'PlatformIO (ファーム)'
    if ($SkipFirmware) { Write-Skip '--skip-firmware'; return }
    $hasPio = $false
    if (Test-CommandExists 'uv') {
        $tools = Get-NativeOutput 'uv' @('tool', 'list')
        if ($tools -match '(?m)^platformio\b') { $hasPio = $true }
    }
    if ($hasPio) {
        Write-Skip 'platformio は導入済み (uv tool)'
    } else {
        Invoke-NativeChecked 'uv' @('tool', 'install', 'platformio') -Capture -FailMsg 'PlatformIO の導入に失敗しました'
        Add-ToolPaths
    }
    $pio = 'pio'
    $pioCmd = Get-Command pio -ErrorAction SilentlyContinue
    if ($pioCmd) { $pio = $pioCmd.Source }
    elseif ($env:USERPROFILE -and (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.local\bin\pio.exe'))) { $pio = Join-Path $env:USERPROFILE '.local\bin\pio.exe' }
    foreach ($fw in @('firmware', 'firmware_remote')) {
        Invoke-NativeChecked $pio @('pkg', 'install', '-d', $fw) -FailMsg "pio pkg install -d $fw に失敗しました"
    }
    Write-Host '[info] CoreS3 (ESP32-S3 の USB CDC) はドライバ不要です'
    $script:ShowFtdiNote = $true
}

function Step10-Firewall {
    param([hashtable]$Cfg)
    Write-Step 10 'ネットワーク (Windows ファイアウォール)'
    $port = $Cfg['HUB_PORT']
    if (-not (Get-Command Get-NetFirewallRule -ErrorAction SilentlyContinue)) {
        Write-Warn 'Get-NetFirewallRule が使えないため確認できません'
        return
    }
    $rule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    if ($rule) {
        Write-Skip "規則 '$FirewallRuleName' は既にあります"
        return
    }
    $cmd = "New-NetFirewallRule -DisplayName '$FirewallRuleName' -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private -ErrorAction Stop | Out-Null"
    if ($DryRun) {
        Write-Host "[dry-run] (管理者) $cmd"
        return
    }
    Write-Host "TCP $port の受信を許可する規則を作ります (UAC の確認が 1 回出ます。プライベートネットワークのみ)"
    try {
        # 引数の引用符が崩れないよう -EncodedCommand (UTF-16LE の Base64) で渡す
        $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
        $p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList '-NoProfile', '-EncodedCommand', $enc
        if ($p -and $p.ExitCode -ne 0) { Write-Warn "規則の作成が失敗しました (終了コード $($p.ExitCode))" }
    } catch {
        Write-Warn "規則を作れませんでした (UAC がキャンセルされた可能性があります): $($_.Exception.Message)"
    }
    if (Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue) {
        Write-Ok "規則 '$FirewallRuleName' (TCP $port、プライベート) を作りました"
    } else {
        Write-Warn 'ファイアウォール規則がありません。ロボットから接続できない可能性があります (検査で不合格になります)'
    }
}

function Step11-Doctor {
    param([hashtable]$Cfg)
    Write-Step 11 '検査 (openmovehub.doctor)'
    $port = $Cfg['HUB_PORT']
    if ($DryRun) {
        Write-Host "[dry-run] $(Format-Cmd 'uv' (Get-DoctorArgs))"
        Write-Host ''
        Write-Host 'dry-run のため何も変更していません。'
        return 0
    }
    if (-not (Test-CommandExists 'uv')) {
        Write-Warn 'uv が見つかりません'
        $code = 1
    } else {
        $code = Invoke-Native 'uv' (Get-DoctorArgs) -Capture -Always
    }
    Write-Host ''
    if ($code -eq 0) {
        Write-Host 'SETUP OK' -ForegroundColor Green
        Write-Host '起動方法: start_windows.bat をダブルクリック'
        Write-HubUrls $port
    } else {
        Write-Host 'SETUP INCOMPLETE' -ForegroundColor Red
        Write-Host '上の表で FAIL の項目を確認し、対処してから setup_windows.bat をもう一度実行してください。'
    }
    if ($script:ShowFtdiNote) {
        Write-Host "[info] M5StickC Plus (リモコン) が COM ポートとして認識されない場合は FTDI VCP ドライバを入れてください: $FtdiUrl"
    }
    if ($code -eq 0) { return 0 }
    return 1
}

# ---- 本体 ----
$transcript = $false
try {
    if (-not $DryRun) {
        $logDir = Join-Path $RepoRoot 'logs'
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $logPath = Join-Path $logDir ("setup_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
        try { Start-Transcript -Path $logPath | Out-Null; $transcript = $true } catch { Write-Warn "ログを開始できませんでした: $($_.Exception.Message)" }
    }

    $mode = @()
    if ($Check) { $mode += '--check' }
    if ($DryRun) { $mode += '--dry-run' }
    if ($SkipVlm) { $mode += '--skip-vlm' }
    if ($SkipFirmware) { $mode += '--skip-firmware' }
    if ($VoicevoxGpu) { $mode += '--voicevox-gpu' }
    if ($VlmModelArg) { $mode += "--vlm-model $VlmModelArg" }
    Write-Host "Open StackChan Move Hub セットアップ (Windows)  $($mode -join ' ')"
    Write-Host "リポジトリ: $RepoRoot"
    Write-Host "固定する版: VOICEVOX ENGINE $($V['VOICEVOX_VERSION'])、Python $($V['PYTHON_VERSION'])、Ollama >= $($V['OLLAMA_MIN_VERSION'])"

    if (-not ($Yes -or $Check -or $DryRun)) {
        $ans = Read-Host '約 15 GB をダウンロードします。続けますか [Y/n]'
        if ($ans -match '^\s*[nN]') {
            Write-Host '中止しました。'
            $script:ExitCode = 1
            throw 'SETUP_ABORT'
        }
    }

    Step1-Precheck

    if ($Check) {
        for ($n = 2; $n -le 10; $n++) { Write-Step $n '(--check のため導入しない)'; Write-Skip '--check' }
        $cfg = Get-HubConfig $RepoRoot $V
        $script:ExitCode = Step11-Doctor $cfg
    } else {
        Step2-OsPackages
        Step3-Uv
        Step4-PythonDeps
        Step5-ConfigFiles
        $cfg = Get-HubConfig $RepoRoot $V
        $model = $cfg['VLM_MODEL']
        if ($VlmModelArg) { $model = $VlmModelArg }
        Step6-Whisper $cfg
        Step7-Voicevox $cfg
        Step8-Ollama $cfg $model
        Step9-PlatformIO
        Step10-Firewall $cfg
        $script:ExitCode = Step11-Doctor $cfg
    }
} catch {
    if ([string]$_.Exception.Message -ne 'SETUP_ABORT') {
        Write-Host "[error] 予期しないエラー: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host $_.ScriptStackTrace
        $script:ExitCode = 1
    }
} finally {
    if ($transcript) { try { Stop-Transcript | Out-Null } catch { } }
}
exit $script:ExitCode
