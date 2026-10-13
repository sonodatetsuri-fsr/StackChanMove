# Windows 用スクリプト (setup / start / flash) の共通関数。各スクリプトから dot-source する。
# 文字コード: UTF-8 (BOM 付き)・CRLF。Windows PowerShell 5.1 と PowerShell 7 の両方で動くこと。

function Initialize-Console {
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    $global:OutputEncoding = [Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
}

function Get-RepoRoot {
    # scripts\ の親がリポジトリのルート
    return (Split-Path -Parent $PSScriptRoot)
}

function Read-KeyValueFile {
    # KEY=VALUE 形式のファイルを hashtable にする (# で始まる行と空行は無視、無ければ空)
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $map }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $idx = $t.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $t.Substring(0, $idx).Trim()
        $val = $t.Substring($idx + 1).Trim()
        if ($val.Length -ge 2 -and (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $map[$key] = $val
    }
    return $map
}

function Read-Versions {
    param([string]$Root)
    return (Read-KeyValueFile (Join-Path $Root 'scripts\versions.env'))
}

function Get-HubConfig {
    # 環境変数 > .env > .env.example > 既定値 の順で Hub の設定を決める
    param([string]$Root, [hashtable]$Versions)
    $envFile = Read-KeyValueFile (Join-Path $Root '.env')
    $example = Read-KeyValueFile (Join-Path $Root '.env.example')
    $port = '8341'
    if ($Versions -and $Versions.ContainsKey('HUB_PORT_DEFAULT')) { $port = $Versions['HUB_PORT_DEFAULT'] }
    $vlmModel = 'qwen3.5:9b'
    if ($Versions -and $Versions.ContainsKey('VLM_MODEL_DEFAULT')) { $vlmModel = $Versions['VLM_MODEL_DEFAULT'] }
    $defaults = [ordered]@{
        HUB_PORT     = $port
        STT_PLUGIN   = 'whisper'
        TTS_PLUGIN   = 'voicevox'
        VLM_BACKEND  = 'ollama'
        VLM_MODEL    = $vlmModel
        VOICEVOX_URL = 'http://127.0.0.1:50021'
        VLM_URL      = 'http://127.0.0.1:11434'
    }
    $cfg = @{}
    foreach ($key in $defaults.Keys) {
        $fromEnv = [Environment]::GetEnvironmentVariable($key)
        if (-not [string]::IsNullOrEmpty($fromEnv)) { $cfg[$key] = $fromEnv }
        elseif ($envFile.ContainsKey($key) -and $envFile[$key] -ne '') { $cfg[$key] = $envFile[$key] }
        elseif ($example.ContainsKey($key) -and $example[$key] -ne '') { $cfg[$key] = $example[$key] }
        else { $cfg[$key] = $defaults[$key] }
    }
    $cfg['VOICEVOX_URL'] = $cfg['VOICEVOX_URL'].TrimEnd('/')
    $cfg['VLM_URL'] = $cfg['VLM_URL'].TrimEnd('/')
    $cfg['STT_PLUGIN'] = $cfg['STT_PLUGIN'].ToLowerInvariant()
    $cfg['TTS_PLUGIN'] = $cfg['TTS_PLUGIN'].ToLowerInvariant()
    $cfg['VLM_BACKEND'] = $cfg['VLM_BACKEND'].ToLowerInvariant()
    return $cfg
}

function Add-PathFront {
    # 現在のプロセスの PATH の先頭にディレクトリを足す (存在し、未登録の場合のみ)
    param([string]$Dir)
    if ([string]::IsNullOrEmpty($Dir)) { return }
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) { return }
    $sep = [IO.Path]::PathSeparator
    $parts = @($env:Path -split [regex]::Escape([string]$sep))
    foreach ($p in $parts) {
        if ($p.TrimEnd('\') -ieq $Dir.TrimEnd('\')) { return }
    }
    $env:Path = $Dir + $sep + $env:Path
}

function Add-ToolPaths {
    # uv (と uv tool の pio) と Ollama の既定のインストール先を PATH に足す
    if ($env:USERPROFILE) { Add-PathFront (Join-Path $env:USERPROFILE '.local\bin') }
    if ($env:LOCALAPPDATA) { Add-PathFront (Join-Path $env:LOCALAPPDATA 'Programs\Ollama') }
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-HttpOk {
    param([string]$Url, [int]$TimeoutSec = 3)
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
    } catch {
        return $false
    }
}

function Wait-HttpOk {
    # URL が応答するまで最大 Seconds 秒待つ。応答すれば $true
    param([string]$Url, [int]$Seconds = 60, $Process = $null)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpOk $Url 2) { return $true }
        if ($null -ne $Process -and $Process.HasExited) { return $false }
        Start-Sleep -Seconds 1
    }
    return (Test-HttpOk $Url 2)
}

function Get-LanIPv4 {
    # LAN の IPv4 アドレス一覧 (127.* と 169.254.* を除く)
    $ips = @()
    if (Get-Command Get-NetIPAddress -ErrorAction SilentlyContinue) {
        try {
            $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | ForEach-Object { $_.IPAddress })
        } catch { $ips = @() }
    }
    if ($ips.Count -eq 0) {
        try {
            $ips = @([System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
                Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
                ForEach-Object { $_.ToString() })
        } catch { $ips = @() }
    }
    return @($ips | Where-Object { $_ -notlike '127.*' -and $_ -notlike '169.254.*' } | Sort-Object -Unique)
}

function Get-VoicevoxDir {
    param([string]$Root, [hashtable]$Versions)
    return (Join-Path $Root ('third_party\voicevox_engine\' + $Versions['VOICEVOX_VERSION']))
}

function Get-OllamaExe {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if ($env:LOCALAPPDATA) {
        $p = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Write-HubUrls {
    # ブラウザで開く URL と端末の設定 GUI に入れる Hub URL を表示する
    param([string]$Port)
    Write-Host "  ブラウザ: http://localhost:$Port/"
    $ips = @(Get-LanIPv4)
    if ($ips.Count -eq 0) {
        Write-Host '  (LAN の IPv4 アドレスが見つかりません。ネットワーク接続を確認してください)'
    }
    foreach ($ip in $ips) {
        Write-Host "  LAN: http://${ip}:$Port/   端末の Hub URL: ws://${ip}:$Port/ws/device"
    }
}
