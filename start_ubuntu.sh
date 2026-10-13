#!/usr/bin/env bash
# VOICEVOX ENGINE・Ollama・Hub をまとめて起動してブラウザを開く (Ubuntu)。
#   bash start_ubuntu.sh               # 起動してブラウザを開く (Ctrl+C で終了)
#   bash start_ubuntu.sh --no-browser  # ブラウザを開かない
# 終了時は Hub と、このスクリプトが起動した VOICEVOX / Ollama だけを止める (元から動いていたものは止めない)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/setup_ubuntu_lib.sh
source "$SCRIPT_DIR/scripts/setup_ubuntu_lib.sh"

open_browser=1
for arg in "$@"; do
	case $arg in
	--no-browser) open_browser=0 ;;
	-h | --help)
		echo "使い方: bash start_ubuntu.sh [--no-browser]"
		exit 0
		;;
	*)
		echo "不明なオプション: $arg" >&2
		echo "使い方: bash start_ubuntu.sh [--no-browser]" >&2
		exit 2
		;;
	esac
done

cd "$ROOT_DIR"
mkdir -p "$LOG_DIR"

hub_port=$(cfg_get HUB_PORT "$HUB_PORT_DEFAULT")
tts_plugin=$(cfg_get TTS_PLUGIN voicevox)
voicevox_url=$(cfg_get VOICEVOX_URL "http://127.0.0.1:$VOICEVOX_PORT")
voicevox_url=${voicevox_url%/}
vlm_backend=$(cfg_get VLM_BACKEND ollama)
vlm_url=$(cfg_get VLM_URL http://127.0.0.1:11434)
vlm_url=${vlm_url%/}

# 起動したプロセス (setsid で別プロセスグループにし、グループごと止める)
hub_pid=""
voicevox_pid=""
ollama_pid=""

# shellcheck disable=SC2329  # trap の cleanup から呼ぶ
stop_group() {
	local pid=$1 name=$2 i
	[[ -n $pid ]] || return 0
	kill -0 "$pid" 2>/dev/null || return 0
	echo "$name を停止します"
	kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
	for ((i = 0; i < 10; i++)); do
		kill -0 "$pid" 2>/dev/null || return 0
		sleep 1
	done
	kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

cleaned=0
# shellcheck disable=SC2329  # trap EXIT から呼ぶ
cleanup() {
	((cleaned)) && return 0
	cleaned=1
	trap - INT TERM
	stop_group "$hub_pid" "Hub"
	stop_group "$voicevox_pid" "VOICEVOX ENGINE"
	stop_group "$ollama_pid" "Ollama"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 1. VOICEVOX ENGINE
if [[ $tts_plugin == voicevox ]]; then
	if http_ok "$voicevox_url/version"; then
		echo "VOICEVOX ENGINE: 起動済み ($voicevox_url)"
	else
		run_bin="$THIRD_PARTY_DIR/voicevox_engine/$VOICEVOX_VERSION/run"
		if [[ -x $run_bin ]]; then
			vv_port=$(url_port "$voicevox_url" "$VOICEVOX_PORT")
			echo "VOICEVOX ENGINE を起動します (ログ: logs/voicevox.log)"
			setsid "$run_bin" --host 127.0.0.1 --port "$vv_port" >>"$LOG_DIR/voicevox.log" 2>&1 &
			voicevox_pid=$!
			if wait_http "$voicevox_url/version" 60; then
				echo "VOICEVOX ENGINE: 起動しました ($voicevox_url)"
			else
				echo "[warn] VOICEVOX ENGINE が 60 s 以内に応答しません (logs/voicevox.log を確認してください)。続行します。" >&2
			fi
		else
			echo "[warn] VOICEVOX ENGINE が見つかりません ($run_bin)。bash setup_ubuntu.sh で導入してください。音声合成なしで続行します。" >&2
		fi
	fi
fi

# 2. Ollama
if [[ $vlm_backend != none ]]; then
	if http_ok "$vlm_url/api/version"; then
		echo "Ollama: 起動済み ($vlm_url)"
	elif command -v ollama >/dev/null 2>&1; then
		echo "Ollama を起動します (ログ: logs/ollama.log)"
		setsid ollama serve >>"$LOG_DIR/ollama.log" 2>&1 &
		ollama_pid=$!
		if wait_http "$vlm_url/api/version" 60; then
			echo "Ollama: 起動しました ($vlm_url)"
		else
			echo "[warn] Ollama が 60 s 以内に応答しません (logs/ollama.log を確認してください)。続行します。" >&2
		fi
	else
		echo "[warn] ollama が見つかりません。bash setup_ubuntu.sh で導入してください。VLM なしで続行します。" >&2
	fi
fi

# 3. CUDA ライブラリ (uv sync --group cuda で入れた cuBLAS / cuDNN)
nvidia_dir="$ROOT_DIR/.venv/lib/python3.12/site-packages/nvidia"
for lib in cublas cudnn; do
	if [[ -d $nvidia_dir/$lib/lib ]]; then
		LD_LIBRARY_PATH="$nvidia_dir/$lib/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
	fi
done
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"

# 4. Hub (画面と logs/hub.log の両方に出す。tee は Ctrl+C を無視して Hub の終了ログまで出す)
echo "Hub を起動します: http://0.0.0.0:${hub_port}/ (ログ: logs/hub.log)"
setsid uv run uvicorn openmovehub.main:app --host 0.0.0.0 --port "$hub_port" \
	> >(
		trap '' INT
		exec tee -a "$LOG_DIR/hub.log"
	) 2>&1 &
hub_pid=$!

# 5. 応答を待って URL を表示し、ブラウザを開く
ready=0
for ((i = 0; i < 120; i++)); do
	if ! kill -0 "$hub_pid" 2>/dev/null; then
		break
	fi
	if http_ok "http://127.0.0.1:${hub_port}/status"; then
		ready=1
		break
	fi
	sleep 1
done
if ((!ready)); then
	echo "[error] Hub が起動しませんでした (logs/hub.log を確認してください)。" >&2
	exit 1
fi

echo
echo "Hub started"
echo "ブラウザで開く URL:"
echo "  http://localhost:${hub_port}/"
while IFS= read -r ip; do
	[[ -n $ip ]] && echo "  http://${ip}:${hub_port}/   (ロボットの Hub URL: ws://${ip}:${hub_port}/ws/device)"
done < <(lan_ipv4s)
echo "終了するには Ctrl+C を押してください。"
echo

if ((open_browser)); then
	if command -v xdg-open >/dev/null 2>&1 && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
		xdg-open "http://localhost:${hub_port}/" >/dev/null 2>&1 || true
	else
		echo "ブラウザで http://localhost:${hub_port}/ を開いてください。"
	fi
fi

# 6. Hub が終わるまで待つ
rc=0
wait "$hub_pid" || rc=$?
exit "$rc"
