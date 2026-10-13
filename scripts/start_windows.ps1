# VOICEVOX ENGINE・Ollama・Hub をまとめて起動し、ブラウザを開く。入口は start_windows.bat。
# 文字コード: UTF-8 (BOM 付き)・CRLF。Windows PowerShell 5.1 と PowerShell 7 の両方で動くこと。
# 使い方: start_windows.bat [--no-browser]

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common_windows.ps1')
Initialize-Console

function Show-Usage {
    Write-Host @'
使い方: start_windows.bat [オプション]

  --no-browser   ブラウザを開かない
  -h, --help     この説明を表示する
'@
}

$NoBrowser = $false
foreach ($a in $args) {
    switch -CaseSensitive ([string]$a) {
        '--no-browser' { $NoBrowser = $true }
        '-h' { Show-Usage; exit 0 }
        '--help' { Show-Usage; exit 0 }
        default { Write-Host "不明なオプション: $a"; Show-Usage; exit 2 }
    }
}

$RepoRoot = Get-RepoRoot
Set-Location -LiteralPath $RepoRoot
$V = Read-Versions $RepoRoot
$Cfg = Get-HubConfig $RepoRoot $V
$Port = $Cfg['HUB_PORT']
Add-ToolPaths
$env:PYTHONUNBUFFERED = '1'

$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Test-CommandExists 'uv')) {
    Write-Host '[error] uv が見つかりません。先に setup_windows.bat を実行してください。' -ForegroundColor Red
    exit 1
}

# このスクリプトが起動したプロセスだけを終了時に止める
$Started = New-Object System.Collections.ArrayList
$Helper = $null
$HubCode = 0

try {
    # 1) VOICEVOX ENGINE
    if ($Cfg['TTS_PLUGIN'] -eq 'voicevox') {
        $vvUrl = $Cfg['VOICEVOX_URL']
        if (Test-HttpOk "$vvUrl/version") {
            Write-Host "[ok] VOICEVOX ENGINE は起動済み ($vvUrl)"
        } else {
            $runExe = Join-Path (Get-VoicevoxDir $RepoRoot $V) 'run.exe'
            if (-not (Test-Path -LiteralPath $runExe)) {
                Write-Host "[warn] VOICEVOX ENGINE が見つかりません ($runExe)。setup_windows.bat を実行してください。音声合成なしで続けます。" -ForegroundColor Yellow
            } else {
                $vvPort = ([uri]$vvUrl).Port
                Write-Host "VOICEVOX ENGINE を起動します (127.0.0.1:$vvPort、ログ logs\voicevox.log)"
                # -NoNewWindow でこのコンソールに紐づけ、ウィンドウを閉じたときに一緒に終了させる
                $p = Start-Process -FilePath $runExe -ArgumentList '--host', '127.0.0.1', '--port', "$vvPort" `
                    -WorkingDirectory (Split-Path -Parent $runExe) -NoNewWindow -PassThru `
                    -RedirectStandardOutput (Join-Path $LogDir 'voicevox.log') -RedirectStandardError (Join-Path $LogDir 'voicevox.err.log')
                [void]$Started.Add($p)
                if (Wait-HttpOk "$vvUrl/version" 60 $p) {
                    Write-Host '[ok] VOICEVOX ENGINE が応答しました'
                } else {
                    Write-Host '[warn] VOICEVOX ENGINE が 60 s 以内に応答しません (logs\voicevox.err.log を確認してください)' -ForegroundColor Yellow
                }
            }
        }
    }

    # 2) Ollama
    if ($Cfg['VLM_BACKEND'] -ne 'none') {
        $apiUrl = "$($Cfg['VLM_URL'])/api/version"
        if (Test-HttpOk $apiUrl) {
            Write-Host "[ok] Ollama は起動済み ($($Cfg['VLM_URL']))"
        } else {
            $ollama = Get-OllamaExe
            if (-not $ollama) {
                Write-Host '[warn] ollama が見つかりません。VLM なしで続けます (setup_windows.bat で導入できます)。' -ForegroundColor Yellow
            } else {
                Write-Host 'ollama serve を起動します (ログ logs\ollama.log)'
                $p = Start-Process -FilePath $ollama -ArgumentList 'serve' -NoNewWindow -PassThru `
                    -RedirectStandardOutput (Join-Path $LogDir 'ollama.log') -RedirectStandardError (Join-Path $LogDir 'ollama.err.log')
                [void]$Started.Add($p)
                if (Wait-HttpOk $apiUrl 60 $p) {
                    Write-Host '[ok] Ollama が応答しました'
                } else {
                    Write-Host '[warn] Ollama が 60 s 以内に応答しません (logs\ollama.err.log を確認してください)' -ForegroundColor Yellow
                }
            }
        }
    }

    # 3) Linux の LD_LIBRARY_PATH に相当する処理は不要 (Whisper プラグインが os.add_dll_directory で行う)

    # 4) Hub が応答したら URL を表示してブラウザを開くヘルパー (先に起動しておく)
    $ipLines = @(Get-LanIPv4 | ForEach-Object { "Write-Host '  http://${_}:$Port/   (端末の Hub URL: ws://${_}:$Port/ws/device)'" }) -join "`n"
    $openBrowser = '$false'
    if (-not $NoBrowser) { $openBrowser = '$true' }
    $helperScript = @"
`$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
`$deadline = (Get-Date).AddSeconds(120)
`$ok = `$false
while ((Get-Date) -lt `$deadline) {
    try { Invoke-WebRequest -Uri 'http://127.0.0.1:$Port/status' -UseBasicParsing -TimeoutSec 2 | Out-Null; `$ok = `$true; break } catch { Start-Sleep -Milliseconds 500 }
}
if (`$ok) {
    Write-Host ''
    Write-Host 'Hub started'
    Write-Host '  http://localhost:$Port/'
$ipLines
    if ($openBrowser) { Start-Process 'http://localhost:$Port/' }
} else {
    Write-Host '[warn] Hub が 120 s 以内に応答しません (logs\hub.log を確認してください)'
}
"@
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($helperScript))
    $Helper = Start-Process -FilePath 'powershell' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $enc -NoNewWindow -PassThru

    # 5) Hub (前面で実行、画面と logs\hub.log の両方に出す)
    Write-Host "Hub を起動します (0.0.0.0:$Port、ログ logs\hub.log、終了は Ctrl+C またはウィンドウを閉じる)"
    $hubLog = New-Object IO.StreamWriter((Join-Path $LogDir 'hub.log'), $true, (New-Object Text.UTF8Encoding $false))
    $hubLog.AutoFlush = $true
    # PS 5.1 では 2>&1 の stderr が ErrorRecord になるため、この部分だけ Continue にする
    $ErrorActionPreference = 'Continue'
    try {
        & uv run uvicorn openmovehub.main:app --host 0.0.0.0 --port $Port 2>&1 | ForEach-Object {
            $line = [string]$_
            Write-Host $line
            $hubLog.WriteLine($line)
        }
        $HubCode = $LASTEXITCODE
    } finally {
        $hubLog.Dispose()
        $ErrorActionPreference = 'Stop'
    }
} finally {
    # 6) このスクリプトが起動したプロセスだけを止める (元から動いていたものは止めない)
    if ($null -ne $Helper -and -not $Helper.HasExited) {
        Stop-Process -Id $Helper.Id -Force -ErrorAction SilentlyContinue
    }
    foreach ($p in $Started) {
        if ($null -ne $p -and -not $p.HasExited) {
            Write-Host "停止します: $($p.ProcessName) (PID $($p.Id))"
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($null -eq $HubCode) { $HubCode = 0 }
exit $HubCode
