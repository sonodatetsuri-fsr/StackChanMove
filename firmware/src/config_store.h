#pragma once

#include <Arduino.h>

/// NVS 名前空間 "stackchan" を読み書きモードで開く。
void config_store_init();

/// wifi_ssid と hub_url がともに空でなければ true。
bool config_store_has_config();

String config_store_get_ssid();
String config_store_get_password();
String config_store_get_hub_url();

/// WiFi 設定と Hub URL を NVS に保存する。
void config_store_save(const String& ssid, const String& password, const String& hub_url);

/// NVS 名前空間 "stackchan" の内容を全て消去する。
void config_store_clear();
