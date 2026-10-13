#pragma once

#include <M5Unified.h>
#include <WiFi.h>

/// WiFi に接続する（タイムアウト約 30 秒）。
/// 接続成功時は IP アドレスを Serial に出力する。
/// @return true: 接続成功, false: タイムアウト
bool wifi_init(const char* ssid, const char* password);

/// ループ内で呼び出し、切断時に自動再接続を試みる。
void wifi_ensure_connected();
