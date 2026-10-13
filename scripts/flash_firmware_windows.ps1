# ファームウェアの書き込み (CoreS3 = core / リモコン M5StickC Plus = remote)。入口は flash_firmware_windows.bat。
# 文字コード: UTF-8 (BOM 付き)・CRLF。Windows PowerShell 5.1 と PowerShell 7 の両方で動くこと。
# 使い方: flash_firmware_windows.bat [core|remote]

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common_windows.ps1')
Initialize-Console

function Show-Usage {
    Write-Host @'
使い方: flash_firmware_windows.bat [core|remote]

  core     CoreS3 (ロボット本体) に firmware を書き込む (既定)
  remote   M5StickC Plus (リモコン) に firmware_remote を書き込む
'@
}

$Target = 'core'
if ($args.Count -gt 1) { Show-Usage; exit 2 }
if ($args.Count -eq 1) {
    switch -CaseSensitive ([string]$args[0]) {
        'core' { $Target = 'core' }
        'remote' { $Target = 'remote' }
        '-h' { Show-Usage; exit 0 }
        '--help' { Show-Usage; exit 0 }
        default { Write-Host "不明な引数: $($args[0])"; Show-Usage; exit 2 }
    }
}
$FwDir = 'firmware'
if ($Target -eq 'remote') { $FwDir = 'firmware_remote' }

$RepoRoot = Get-RepoRoot
Set-Location -LiteralPath $RepoRoot
$V = Read-Versions $RepoRoot
$Cfg = Get-HubConfig $RepoRoot $V
$Port = $Cfg['HUB_PORT']
Add-ToolPaths

# config.h が無ければ example からコピー
$cfgH = Join-Path $RepoRoot "$FwDir\include\config.h"
if (-not (Test-Path -LiteralPath $cfgH)) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "$FwDir\include\config.h.example") -Destination $cfgH
    Write-Host "[ok] $FwDir\include\config.h を config.h.example から作りました"
}

# USB のシリアルポートを探す
$ports = @()
try {
    $ports += @((Get-ItemProperty -Path 'HKLM:\HARDWARE\DEVICEMAP\SERIALCOMM' -ErrorAction Stop).PSObject.Properties |
        Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object { [string]$_.Value })
} catch { }
try { $ports += @([System.IO.Ports.SerialPort]::GetPortNames()) } catch { }
if ($ports.Count -eq 0) {
    try { $ports += @(Get-CimInstance Win32_SerialPort -ErrorAction Stop | ForEach-Object { $_.DeviceID }) } catch { }
}
$ports = @($ports | Where-Object { $_ } | Sort-Object -Unique)
if ($ports.Count -eq 0) {
    Write-Host '[error] シリアルポートが見つかりません。USB ケーブルでつないでください (充電専用ケーブルでは認識しません)。' -ForegroundColor Red
    if ($Target -eq 'remote') {
        Write-Host '  M5StickC Plus が認識されない場合は FTDI VCP ドライバを入れてください: https://ftdichip.com/drivers/vcp-drivers/'
    }
    exit 1
}
Write-Host "シリアルポート: $($ports -join ', ')"

# pio を探す (PATH → ~\.local\bin\pio.exe → uv tool run)
$exe = $null
$pre = @()
$pioCmd = Get-Command pio -ErrorAction SilentlyContinue
if ($pioCmd) {
    $exe = $pioCmd.Source
} elseif ($env:USERPROFILE -and (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.local\bin\pio.exe'))) {
    $exe = Join-Path $env:USERPROFILE '.local\bin\pio.exe'
} elseif (Test-CommandExists 'uv') {
    $exe = 'uv'
    $pre = @('tool', 'run', '--from', 'platformio', 'pio')
} else {
    Write-Host '[error] PlatformIO (pio) も uv も見つかりません。先に setup_windows.bat を実行してください。' -ForegroundColor Red
    exit 1
}

$pioArgs = $pre + @('run', '-d', $FwDir, '-t', 'upload')
Write-Host "> $exe $($pioArgs -join ' ')"
$ErrorActionPreference = 'Continue'
& $exe @pioArgs
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($null -eq $code) { $code = 0 }
if ($code -ne 0) {
    Write-Host "[error] 書き込みに失敗しました (終了コード $code)。ケーブルとポートを確認してください。" -ForegroundColor Red
    exit $code
}

Write-Host ''
Write-Host "[ok] $FwDir を書き込みました" -ForegroundColor Green
if ($Target -eq 'remote') {
    Write-Host 'リモコンには WiFi と Hub URL の設定は不要です。'
    exit 0
}
Write-Host '次に、端末の設定 GUI で WiFi と Hub URL を入れてください:'
Write-Host '  1. 端末の画面を長押しして設定 GUI を開く'
Write-Host '  2. WiFi の SSID とパスワードを入れる'
Write-Host '  3. Hub URL に次のいずれか (このPCの LAN IP) を入れる'
$ips = @(Get-LanIPv4)
if ($ips.Count -eq 0) { Write-Host "     ws://<このPCのIP>:$Port/ws/device" }
foreach ($ip in $ips) { Write-Host "     ws://${ip}:$Port/ws/device" }
Write-Host '  4. 保存して再起動し、start_windows.bat で Hub を起動しておく'
exit 0
