#!/usr/bin/env bash
# ファームウェアの書き込み (Ubuntu)。
#   bash flash_firmware_ubuntu.sh          # CoreS3 (firmware)
#   bash flash_firmware_ubuntu.sh remote   # 付属リモコン (firmware_remote)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/setup_ubuntu_lib.sh
source "$SCRIPT_DIR/scripts/setup_ubuntu_lib.sh"

usage() {
	echo "使い方: bash flash_firmware_ubuntu.sh [core|remote]   (既定: core)"
}

target=${1:-core}
case $target in
core) fw_dir=firmware ;;
remote) fw_dir=firmware_remote ;;
-h | --help)
	usage
	exit 0
	;;
*)
	usage >&2
	exit 2
	;;
esac
if (($# > 1)); then
	usage >&2
	exit 2
fi

config="$ROOT_DIR/$fw_dir/include/config.h"
if [[ ! -f $config ]]; then
	cp "$ROOT_DIR/$fw_dir/include/config.h.example" "$config"
	echo "$fw_dir/include/config.h を config.h.example から作りました。"
fi

mapfile -t ports < <(serial_ports)
if ((${#ports[@]} == 0)); then
	echo "USB のシリアルポートが見つかりません。USB ケーブルでつないでください (充電専用ケーブルでは認識しません)。" >&2
	exit 1
fi
echo "シリアルポート: ${ports[*]}"

resolve_pio
echo "+ ${PIO[*]} run -d $fw_dir -t upload"
"${PIO[@]}" run -d "$ROOT_DIR/$fw_dir" -t upload

port=$(cfg_get HUB_PORT "$HUB_PORT_DEFAULT")
echo
echo "書き込みが終わりました。"
if [[ $target == core ]]; then
	echo "次に、端末の設定 GUI で WiFi と Hub URL を入れてください:"
	echo "  1. 画面を長押しして設定 GUI を開く"
	echo "  2. WiFi の SSID とパスワードを入れる"
	echo "  3. Hub URL に次のどれか (このPCの LAN IP) を入れる:"
	while IFS= read -r ip; do
		[[ -n $ip ]] && echo "     ws://${ip}:${port}/ws/device"
	done < <(lan_ipv4s)
	echo "  4. 保存して再起動し、Web クライアント (bash start_ubuntu.sh) で接続を確認する"
else
	echo "リモコンは ESP-NOW で CoreS3 と直接通信するため、Hub URL の設定は不要です (詳細は README.md)。"
fi
