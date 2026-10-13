#!/usr/bin/env bash
# Ubuntu 用セットアップ / 起動スクリプトの共通関数。
# setup_ubuntu.sh・start_ubuntu.sh・flash_firmware_ubuntu.sh から source する (単体では何もしない)。
# shellcheck disable=SC2034  # ここで定義した変数は source 側のスクリプトが使う

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$LIB_DIR/.." && pwd)"
VERSIONS_FILE="$LIB_DIR/versions.env"
LOG_DIR="$ROOT_DIR/logs"
THIRD_PARTY_DIR="$ROOT_DIR/third_party"

# uv / platformio (uv tool) の置き場を常に PATH に入れる
export PATH="$HOME/.local/bin:$PATH"

# shellcheck source=scripts/versions.env
source "$VERSIONS_FILE"

# ---------------------------------------------------------------- 出力

log_info() { printf '%s\n' "$*"; }
log_warn() { printf '[warn] %s\n' "$*" >&2; }
log_error() { printf '[error] %s\n' "$*" >&2; }
log_skip() { printf '[skip] %s\n' "$*"; }
log_ok() { printf '[ok] %s\n' "$*"; }

# ---------------------------------------------------------------- 設定値

# dotenv_get FILE KEY: FILE の KEY=VALUE を読む (source しない)。無ければ非 0。
dotenv_get() {
	local file=$1 key=$2 line value
	[[ -f $file ]] || return 1
	line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n 1) || return 1
	[[ -n $line ]] || return 1
	value=${line#*=}
	value=${value%$'\r'}
	if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then
		value=${BASH_REMATCH[1]}
	fi
	printf '%s\n' "$value"
}

# cfg_get KEY [DEFAULT]: 環境変数 > .env > .env.example > DEFAULT の順で設定値を返す。
cfg_get() {
	local key=$1 default=${2:-}
	if printenv "$key" >/dev/null 2>&1; then
		printenv "$key"
		return 0
	fi
	dotenv_get "$ROOT_DIR/.env" "$key" ||
		dotenv_get "$ROOT_DIR/.env.example" "$key" ||
		printf '%s\n' "$default"
}

# dotenv_set FILE KEY VALUE: KEY の行を置き換える (無ければ末尾に追記)。
dotenv_set() {
	local file=$1 key=$2 value=$3 tmp
	tmp=$(mktemp "${file}.XXXXXX")
	if grep -qE "^[[:space:]]*${key}=" "$file"; then
		awk -v k="$key" -v v="$value" '
			$0 ~ "^[[:space:]]*" k "=" { print k "=" v; next }
			{ print }
		' "$file" >"$tmp"
	else
		cat "$file" >"$tmp"
		[[ -s $tmp && $(tail -c 1 "$tmp") != "" ]] && printf '\n' >>"$tmp"
		printf '%s=%s\n' "$key" "$value" >>"$tmp"
	fi
	cat "$tmp" >"$file"
	rm -f "$tmp"
}

# url_port URL [DEFAULT]: http://host:port/... のポートを返す。
url_port() {
	local url=$1 default=${2:-80} rest
	rest=${url#*://}
	rest=${rest%%/*}
	if [[ $rest == *:* ]]; then
		printf '%s\n' "${rest##*:}"
	else
		printf '%s\n' "$default"
	fi
}

# ---------------------------------------------------------------- 検出

# http_ok URL: 2 s 以内に 2xx が返れば 0。
http_ok() {
	curl -sf --max-time 2 -o /dev/null "$1" 2>/dev/null
}

# wait_http URL SECONDS: 応答するまで最大 SECONDS 秒待つ。
wait_http() {
	local url=$1 secs=$2 i
	for ((i = 0; i < secs; i++)); do
		http_ok "$url" && return 0
		sleep 1
	done
	http_ok "$url"
}

# version_ge A B: A >= B (sort -V で比較)。
version_ge() {
	[[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" == "$2" ]]
}

# lan_ipv4s: LAN の IPv4 アドレス (docker 等の仮想ブリッジを除く) を 1 行ずつ。
lan_ipv4s() {
	local out=""
	if command -v ip >/dev/null 2>&1; then
		out=$(ip -4 -o addr show scope global 2>/dev/null |
			awk '$2 !~ /^(docker|br-|virbr|veth|lxc|cni|flannel|tun|tap)/ { split($4, a, "/"); print a[1] }')
	fi
	if [[ -z $out ]] && command -v hostname >/dev/null 2>&1; then
		out=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' || true)
	fi
	printf '%s\n' "$out" | sed '/^$/d'
}

# find_7z: 7z の実行ファイル名 (7z / 7zz / 7za) を返す。
find_7z() {
	local c
	for c in 7z 7zz 7za; do
		if command -v "$c" >/dev/null 2>&1; then
			printf '%s\n' "$c"
			return 0
		fi
	done
	return 1
}

# pio_cmd: pio の実行コマンドを配列 PIO に入れる。
resolve_pio() {
	if command -v pio >/dev/null 2>&1; then
		PIO=(pio)
	elif [[ -x $HOME/.local/bin/pio ]]; then
		PIO=("$HOME/.local/bin/pio")
	else
		PIO=(uv tool run --from platformio pio)
	fi
}

# serial_ports: USB シリアルポートを 1 行ずつ。
serial_ports() {
	local p
	for p in /dev/ttyACM* /dev/ttyUSB*; do
		[[ -e $p ]] && printf '%s\n' "$p"
	done
	return 0
}

# ollama_version: `ollama --version` の版 (無ければ空)。
ollama_version() {
	command -v ollama >/dev/null 2>&1 || return 0
	ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | tail -n 1 || true
}

# ollama_has_model MODEL: `ollama list` にあれば 0 (タグ省略時は :latest とも比較)。
ollama_has_model() {
	local model=$1 names
	names=$(ollama list 2>/dev/null | awk 'NR > 1 { print $1 }') || return 1
	grep -Fxq -- "$model" <<<"$names" && return 0
	[[ $model != *:* ]] && grep -Fxq -- "$model:latest" <<<"$names"
}

# ================================================================ setup_ubuntu.sh の手順

SETUP_TOTAL_STEPS=11
DRY_RUN=0
CHECK_ONLY=0
SKIP_VLM=0
SKIP_FIRMWARE=0
OPT_VLM_MODEL=""
VOICEVOX_GPU=0
ASSUME_YES=0
HAS_GPU=0
OS_VERSION_ID=""
NEED_RELOGIN=0
SUDO_READY=0
SUDO_KEEPALIVE_PID=""
UV_RUN=()

step_header() {
	printf '\n== [%s/%s] %s ==\n' "$1" "$SETUP_TOTAL_STEPS" "$2"
}

# run CMD...: 実行 (dry-run なら表示だけ)。
run() {
	if ((DRY_RUN)); then
		printf '[dry-run] %s\n' "$*"
		return 0
	fi
	printf '+ %s\n' "$*"
	"$@"
}

# run_sh 'CMD | CMD': パイプを含むコマンド文字列を実行 (dry-run なら表示だけ)。
run_sh() {
	if ((DRY_RUN)); then
		printf '[dry-run] %s\n' "$1"
		return 0
	fi
	printf '+ %s\n' "$1"
	bash -o pipefail -c "$1"
}

as_root() {
	if ((EUID == 0)); then
		"$@"
	else
		sudo "$@"
	fi
}

stop_sudo_keepalive() {
	if [[ -n $SUDO_KEEPALIVE_PID ]]; then
		kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
		SUDO_KEEPALIVE_PID=""
	fi
}

# ensure_sudo: sudo のパスワードを (最初の 1 回だけ) 聞き、60 s ごとに更新し続ける。
ensure_sudo() {
	((EUID == 0 || SUDO_READY)) && return 0
	log_info "管理者権限 (sudo) が必要です。パスワードを入力してください。"
	if ! sudo -v; then
		log_error "sudo の認証に失敗しました。"
		exit 1
	fi
	SUDO_READY=1
	(
		while kill -0 "$$" 2>/dev/null; do
			sudo -n true 2>/dev/null || true
			sleep 60
		done
	) &
	SUDO_KEEPALIVE_PID=$!
}

# run_root CMD...: root で実行 (dry-run なら `sudo CMD` と表示だけ)。
run_root() {
	if ((DRY_RUN)); then
		printf '[dry-run] sudo %s\n' "$*"
		return 0
	fi
	ensure_sudo
	printf '+ sudo %s\n' "$*"
	as_root "$@"
}

setup_usage() {
	cat <<'USAGE'
使い方: bash setup_ubuntu.sh [オプション]

Open StackChan Move Hub に必要なもの (uv・Python と依存・Whisper モデル・VOICEVOX ENGINE・
Ollama と VLM モデル・PlatformIO) をまとめて導入し、最後に検査します。何度実行しても安全です。

オプション:
  --check            導入せず検査だけ行う (手順 1 と 11。sudo 不要、何も変更しない)
  --dry-run          実行する予定のコマンドを表示するだけ (何も変更しない)
  --skip-vlm         Ollama と VLM モデルを導入しない
  --skip-firmware    PlatformIO (ファームの書き込み環境) を導入しない
  --vlm-model TAG    VLM のモデルを TAG にする (.env の VLM_MODEL にも書く)
  --voicevox-gpu     VOICEVOX ENGINE を GPU 版 (NVIDIA) にする
  --yes              最初の確認を出さない
  -h, --help         この説明を表示する
USAGE
}

# setup_parse_args "$@": オプションを解析する。未知のオプションは使い方を出して exit 2。
setup_parse_args() {
	while (($#)); do
		case $1 in
		--check) CHECK_ONLY=1 ;;
		--dry-run) DRY_RUN=1 ;;
		--skip-vlm) SKIP_VLM=1 ;;
		--skip-firmware) SKIP_FIRMWARE=1 ;;
		--vlm-model)
			if (($# < 2)) || [[ -z $2 || $2 == -* ]]; then
				log_error "--vlm-model にはモデル名を指定してください。"
				setup_usage >&2
				exit 2
			fi
			OPT_VLM_MODEL=$2
			shift
			;;
		--vlm-model=*) OPT_VLM_MODEL=${1#*=} ;;
		--voicevox-gpu) VOICEVOX_GPU=1 ;;
		--yes | -y) ASSUME_YES=1 ;;
		-h | --help)
			setup_usage
			exit 0
			;;
		*)
			log_error "不明なオプション: $1"
			setup_usage >&2
			exit 2
			;;
		esac
		shift
	done
}

# ---------------------------------------------------------------- 手順 1: 事前確認

step_preflight() {
	step_header 1 "事前確認"
	local problems=() id="" ver="" arch need free

	if [[ -r /etc/os-release ]]; then
		id=$(dotenv_get /etc/os-release ID || true)
		ver=$(dotenv_get /etc/os-release VERSION_ID || true)
	fi
	OS_VERSION_ID=$ver
	arch=$(uname -m)
	if [[ $id == ubuntu && ($ver == 22.04 || $ver == 24.04) ]]; then
		log_ok "OS: Ubuntu $ver"
	else
		problems+=("対応していない OS です (${id:-不明} ${ver:-}): Ubuntu 22.04 / 24.04 が必要です")
	fi
	if [[ $arch == x86_64 ]]; then
		log_ok "CPU: $arch"
	else
		problems+=("対応していない CPU です ($arch): x86_64 が必要です")
	fi

	need=$MIN_DISK_GB
	((SKIP_VLM)) && need=$MIN_DISK_GB_NO_VLM
	free=$(df -Pk "$ROOT_DIR" | awk 'NR == 2 { print int($4 / 1024 / 1024) }')
	if ((free >= need)); then
		log_ok "空きディスク: ${free} GB (必要 ${need} GB)"
	else
		problems+=("空きディスクが足りません: ${free} GB (必要 ${need} GB)")
	fi

	if ((DRY_RUN)); then
		printf '[dry-run] curl -sfI --max-time 10 https://github.com\n'
	elif curl -sfI --max-time 10 https://github.com >/dev/null 2>&1; then
		log_ok "インターネット接続: https://github.com に接続できます"
	else
		problems+=("インターネットに接続できません (https://github.com に届きません)")
	fi

	if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
		HAS_GPU=1
		log_ok "NVIDIA GPU: あり"
	else
		HAS_GPU=0
		log_info "NVIDIA GPU: なし (Whisper は CPU で動かします)"
	fi

	if ((${#problems[@]})); then
		local p
		for p in "${problems[@]}"; do log_error "$p"; done
		if ((DRY_RUN || CHECK_ONLY)); then
			log_warn "条件を満たしていませんが、--dry-run / --check のため続けます。"
		else
			exit 2
		fi
	fi

	UV_RUN=(uv run --frozen)
	((HAS_GPU)) && UV_RUN+=(--group cuda)
	if ((CHECK_ONLY)); then
		# --check は何も変更しないため、環境の同期 (uv sync) をしない
		UV_RUN=(uv run --no-sync)
	fi
}

# ---------------------------------------------------------------- 手順 2: OS のパッケージ

step_apt() {
	step_header 2 "OS のパッケージ"
	local pkgs=(curl ca-certificates zstd) missing=() p
	if [[ $OS_VERSION_ID == 22.04 ]]; then
		pkgs+=(p7zip-full)
	else
		pkgs+=(7zip)
	fi
	for p in "${pkgs[@]}"; do
		dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
	done
	if ((${#missing[@]} == 0)); then
		log_skip "導入済み: ${pkgs[*]}"
		return 0
	fi
	run_root apt-get update
	run_root apt-get install -y "${missing[@]}"
	((DRY_RUN)) && return 0
	for p in "${missing[@]}"; do
		if ! dpkg -s "$p" >/dev/null 2>&1; then
			log_error "パッケージ $p を導入できませんでした。"
			exit 1
		fi
	done
	log_ok "導入しました: ${missing[*]}"
}

# ---------------------------------------------------------------- 手順 3: uv

step_uv() {
	step_header 3 "uv"
	if command -v uv >/dev/null 2>&1; then
		log_skip "$(uv --version)"
		return 0
	fi
	run_sh "curl -LsSf https://astral.sh/uv/install.sh | sh"
	((DRY_RUN)) && return 0
	export PATH="$HOME/.local/bin:$PATH"
	if ! command -v uv >/dev/null 2>&1; then
		log_error "uv を導入できませんでした。"
		exit 1
	fi
	log_ok "$(uv --version)"
}

# ---------------------------------------------------------------- 手順 4: Python と依存

step_python() {
	step_header 4 "Python と依存"
	local cmd=(uv sync --frozen)
	((HAS_GPU)) && cmd+=(--group cuda)
	(
		cd "$ROOT_DIR"
		run "${cmd[@]}"
	) || {
		log_error "依存の導入に失敗しました。"
		exit 1
	}
	((DRY_RUN)) && return 0
	if ! (cd "$ROOT_DIR" && "${UV_RUN[@]}" python -c "import openmovehub" >/dev/null); then
		log_error "openmovehub を import できません。"
		exit 1
	fi
	log_ok "Python と依存を導入しました"
}

# ---------------------------------------------------------------- 手順 5: 設定ファイル

step_config() {
	step_header 5 "設定ファイル"
	local env_file="$ROOT_DIR/.env" d
	if [[ -f $env_file ]]; then
		log_skip ".env は既にあります (変更しません)"
	else
		run cp "$ROOT_DIR/.env.example" "$env_file"
		if ((!HAS_GPU)); then
			if ((DRY_RUN)); then
				printf '[dry-run] .env の STT_DEVICE を cpu にする\n'
			else
				dotenv_set "$env_file" STT_DEVICE cpu
				log_info ".env: STT_DEVICE=cpu (GPU なし)"
			fi
		fi
	fi
	if [[ -n $OPT_VLM_MODEL ]]; then
		if ((DRY_RUN)); then
			printf '[dry-run] .env の VLM_MODEL を %s にする\n' "$OPT_VLM_MODEL"
		else
			dotenv_set "$env_file" VLM_MODEL "$OPT_VLM_MODEL"
			log_info ".env: VLM_MODEL=$OPT_VLM_MODEL"
		fi
	fi
	for d in firmware firmware_remote; do
		if [[ -f $ROOT_DIR/$d/include/config.h ]]; then
			log_skip "$d/include/config.h は既にあります"
		else
			run cp "$ROOT_DIR/$d/include/config.h.example" "$ROOT_DIR/$d/include/config.h"
		fi
	done
}

# ---------------------------------------------------------------- 手順 6: Whisper モデル

step_whisper() {
	step_header 6 "Whisper モデル"
	local plugin
	plugin=$(cfg_get STT_PLUGIN whisper)
	if [[ $plugin != whisper ]]; then
		log_skip "STT_PLUGIN=$plugin (whisper ではない)"
		return 0
	fi
	(
		cd "$ROOT_DIR"
		run "${UV_RUN[@]}" python -m openmovehub.doctor --prefetch-stt
	) || {
		log_error "Whisper モデルを取得できませんでした。"
		exit 1
	}
}

# ---------------------------------------------------------------- 手順 7: VOICEVOX ENGINE

# voicevox_download PREFIX: versions.env の PREFIX_URL_n を third_party/downloads に取得して検査する。
voicevox_download() {
	local prefix=$1 parts_var="${1}_PARTS" parts i url_var sha_var url sha file rc
	parts=${!parts_var}
	mkdir -p "$THIRD_PARTY_DIR/downloads"
	for ((i = 1; i <= parts; i++)); do
		url_var="${prefix}_URL_$i"
		sha_var="${prefix}_SHA256_$i"
		url=${!url_var}
		sha=${!sha_var}
		file="$THIRD_PARTY_DIR/downloads/$(basename "$url")"
		if [[ -f $file ]] && sha256sum -c --status <<<"$sha  $file"; then
			log_skip "ダウンロード済み: $(basename "$file")"
			continue
		fi
		rc=0
		run curl -fL -C - --retry 5 -o "$file" "$url" || rc=$?
		if ((rc != 0)); then
			if ((rc == 33)); then
				# 途中再開できない (サーバが範囲指定を受け付けない) ので最初から取り直す
				rm -f "$file"
			fi
			log_error "ダウンロードが中断されました (curl の終了コード $rc)。もう一度実行すると続きから再開します。"
			exit 1
		fi
		if ! sha256sum -c --status <<<"$sha  $file"; then
			log_error "SHA256 が一致しません: $(basename "$file") (ファイルを削除しました。もう一度実行してください)"
			rm -f "$file"
			exit 1
		fi
		log_ok "SHA256 一致: $(basename "$file")"
	done
}

step_voicevox() {
	step_header 7 "VOICEVOX ENGINE"
	local plugin flavor prefix parts_var parts i url_var sha_var dest run_bin
	plugin=$(cfg_get TTS_PLUGIN voicevox)
	if [[ $plugin != voicevox ]]; then
		log_skip "TTS_PLUGIN=$plugin (voicevox ではない)"
		return 0
	fi
	if ((VOICEVOX_GPU)); then
		flavor="GPU 版 (NVIDIA)"
		prefix=VOICEVOX_LINUX_GPU
	else
		flavor="CPU 版"
		prefix=VOICEVOX_LINUX_CPU
	fi
	dest="$THIRD_PARTY_DIR/voicevox_engine/$VOICEVOX_VERSION"
	run_bin="$dest/run"
	parts_var="${prefix}_PARTS"
	parts=${!parts_var}
	log_info "VOICEVOX ENGINE $VOICEVOX_VERSION ($flavor, ${parts} 分割) → third_party/voicevox_engine/$VOICEVOX_VERSION/"
	if ((DRY_RUN)); then
		for ((i = 1; i <= parts; i++)); do
			url_var="${prefix}_URL_$i"
			sha_var="${prefix}_SHA256_$i"
			printf '  %s=%s\n' "$url_var" "${!url_var}"
			printf '  %s=%s\n' "$sha_var" "${!sha_var}"
		done
	fi
	if [[ -x $run_bin ]]; then
		log_skip "展開済み: $run_bin"
		return 0
	fi

	if ((DRY_RUN)); then
		for ((i = 1; i <= parts; i++)); do
			url_var="${prefix}_URL_$i"
			printf '[dry-run] curl -fL -C - --retry 5 -o third_party/downloads/%s %s\n' \
				"$(basename "${!url_var}")" "${!url_var}"
		done
		printf '[dry-run] sha256sum で各ファイルを検査\n'
		url_var="${prefix}_URL_1"
		printf '[dry-run] 7z x third_party/downloads/%s -othird_party/voicevox_engine/.extract\n' "$(basename "${!url_var}")"
		printf '[dry-run] mv <展開した run のあるディレクトリ> third_party/voicevox_engine/%s\n' "$VOICEVOX_VERSION"
		printf '[dry-run] third_party/voicevox_engine/%s/run --help\n' "$VOICEVOX_VERSION"
		return 0
	fi

	local sevenz first tmp found
	if ! sevenz=$(find_7z); then
		log_error "7z が見つかりません (手順 2 で 7-Zip を導入してください)。"
		exit 1
	fi
	voicevox_download "$prefix"
	url_var="${prefix}_URL_1"
	first="$THIRD_PARTY_DIR/downloads/$(basename "${!url_var}")"
	mkdir -p "$THIRD_PARTY_DIR/voicevox_engine"
	tmp="$THIRD_PARTY_DIR/voicevox_engine/.extract"
	rm -rf "$tmp"
	mkdir -p "$tmp"
	if ! run "$sevenz" x -y "$first" "-o$tmp" >/dev/null; then
		rm -rf "$tmp"
		log_error "VOICEVOX ENGINE を展開できませんでした。"
		exit 1
	fi
	# 7z が実行ビットを復元しない場合もあるので、名前で探してから実行ビットを付ける
	found=$(find "$tmp" -type f -name run -print -quit)
	if [[ -z $found ]]; then
		rm -rf "$tmp"
		log_error "展開したファイルに run が見つかりません。"
		exit 1
	fi
	chmod +x "$found"
	rm -rf "$dest"
	mv "$(dirname "$found")" "$dest"
	rm -rf "$tmp"
	if ! timeout 120 "$run_bin" --help >/dev/null 2>&1; then
		log_error "$run_bin --help が失敗しました。"
		exit 1
	fi
	# 展開済みなら再実行で skip するため、ダウンロードしたアーカイブは容量節約のため消す
	for ((i = 1; i <= parts; i++)); do
		url_var="${prefix}_URL_$i"
		rm -f "$THIRD_PARTY_DIR/downloads/$(basename "${!url_var}")"
	done
	rmdir "$THIRD_PARTY_DIR/downloads" 2>/dev/null || true
	log_ok "VOICEVOX ENGINE $VOICEVOX_VERSION を展開しました: $dest"
}

# ---------------------------------------------------------------- 手順 8: Ollama

step_ollama() {
	step_header 8 "Ollama"
	local backend url model ver caps
	backend=$(cfg_get VLM_BACKEND ollama)
	if ((SKIP_VLM)); then
		log_skip "--skip-vlm"
		return 0
	fi
	if [[ $backend == none ]]; then
		log_skip "VLM_BACKEND=none"
		return 0
	fi
	url=$(cfg_get VLM_URL http://127.0.0.1:11434)
	url=${url%/}
	if [[ -n $OPT_VLM_MODEL ]]; then
		model=$OPT_VLM_MODEL
	else
		model=$(cfg_get VLM_MODEL "$VLM_MODEL_DEFAULT")
		[[ -n $model ]] || model=$VLM_MODEL_DEFAULT
	fi

	ver=$(ollama_version)
	if [[ -n $ver ]] && version_ge "$ver" "$OLLAMA_MIN_VERSION"; then
		log_skip "Ollama $ver (>= $OLLAMA_MIN_VERSION)"
	else
		if [[ -n $ver ]]; then
			log_info "Ollama $ver は古いため更新します (必要 $OLLAMA_MIN_VERSION 以上)"
		fi
		((DRY_RUN)) || ensure_sudo
		run_sh "curl -fsSL https://ollama.com/install.sh | sh"
		if ((!DRY_RUN)); then
			hash -r
			ver=$(ollama_version)
			if [[ -z $ver ]] || ! version_ge "$ver" "$OLLAMA_MIN_VERSION"; then
				log_error "Ollama を導入できませんでした。"
				exit 1
			fi
			log_ok "Ollama $ver"
		fi
	fi

	if ((DRY_RUN)); then
		if http_ok "$url/api/version"; then
			log_ok "Ollama サーバ応答あり: $url"
			if ollama_has_model "$model"; then
				log_skip "モデル取得済み: $model"
			else
				printf '[dry-run] ollama pull %s\n' "$model"
			fi
		else
			printf '[dry-run] %s/api/version の応答を最大 60 s 待つ (無ければ nohup ollama serve > logs/ollama.log 2>&1 &)\n' "$url"
			printf '[dry-run] ollama pull %s (ollama list に無ければ)\n' "$model"
		fi
		printf '[dry-run] ollama show %s で vision 対応を確認\n' "$model"
		return 0
	fi

	if ! wait_http "$url/api/version" 60; then
		log_info "Ollama が応答しないため ollama serve を起動します (ログ: logs/ollama.log)"
		mkdir -p "$LOG_DIR"
		nohup ollama serve >"$LOG_DIR/ollama.log" 2>&1 &
		if ! wait_http "$url/api/version" 60; then
			log_error "Ollama ($url) が応答しません。logs/ollama.log を確認してください。"
			exit 1
		fi
	fi
	log_ok "Ollama サーバ応答あり: $url"

	if ollama_has_model "$model"; then
		log_skip "モデル取得済み: $model"
	else
		run ollama pull "$model" || {
			log_error "モデル $model を取得できませんでした。"
			exit 1
		}
	fi
	caps=$(ollama show "$model" 2>/dev/null | awk '
		/^[[:space:]]*Capabilities/ { in_caps = 1; next }
		in_caps && /^[[:space:]]*$/ { in_caps = 0 }
		in_caps { print $1 }
	')
	if ! grep -qx vision <<<"$caps"; then
		log_error "画像に対応していないモデルです: $model (Capabilities に vision がありません)"
		exit 2
	fi
	log_ok "モデル $model (vision 対応)"
}

# ---------------------------------------------------------------- 手順 9: PlatformIO

step_platformio() {
	step_header 9 "PlatformIO (ファーム)"
	if ((SKIP_FIRMWARE)); then
		log_skip "--skip-firmware"
		return 0
	fi
	if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -qE '^platformio( |$)'; then
		log_skip "platformio は導入済み (uv tool)"
	else
		run uv tool install platformio || {
			log_error "PlatformIO を導入できませんでした。"
			exit 1
		}
	fi
	local pio="$HOME/.local/bin/pio" d
	command -v pio >/dev/null 2>&1 && pio=$(command -v pio)
	for d in firmware firmware_remote; do
		run "$pio" pkg install -d "$ROOT_DIR/$d" || {
			log_error "$d のツールチェーンを取得できませんでした。"
			exit 1
		}
	done

	# udev ルール
	local rules=/etc/udev/rules.d/99-platformio-udev.rules tmp
	if ((DRY_RUN)); then
		printf '[dry-run] curl -fsSL %s で取得し %s と違えば:\n' "$PIO_UDEV_RULES_URL" "$rules"
		printf '[dry-run] sudo install -m 644 <取得したファイル> %s\n' "$rules"
		printf '[dry-run] sudo udevadm control --reload-rules && sudo udevadm trigger\n'
	else
		tmp=$(mktemp)
		if ! curl -fsSL --retry 3 -o "$tmp" "$PIO_UDEV_RULES_URL"; then
			rm -f "$tmp"
			log_error "udev ルールを取得できませんでした: $PIO_UDEV_RULES_URL"
			exit 1
		fi
		if cmp -s "$tmp" "$rules"; then
			log_skip "udev ルールは最新です: $rules"
		else
			run_root install -m 644 "$tmp" "$rules"
			run_root udevadm control --reload-rules
			run_root udevadm trigger
		fi
		rm -f "$tmp"
	fi

	# シリアルポートを使うためのグループ
	local user groups missing=() g
	user=${USER:-$(id -un)}
	groups=" $(id -nG "$user" 2>/dev/null) "
	for g in dialout plugdev; do
		getent group "$g" >/dev/null 2>&1 || continue
		[[ $groups == *" $g "* ]] || missing+=("$g")
	done
	if ((${#missing[@]} == 0)); then
		log_skip "$user は dialout / plugdev に所属済み"
	else
		local joined
		joined=$(
			IFS=,
			printf '%s' "${missing[*]}"
		)
		run_root usermod -aG "$joined" "$user"
		NEED_RELOGIN=1
	fi
}

# ---------------------------------------------------------------- 手順 10: ネットワーク

step_network() {
	step_header 10 "ネットワーク"
	local port active=0 status=""
	port=$(cfg_get HUB_PORT "$HUB_PORT_DEFAULT")
	if ! command -v ufw >/dev/null 2>&1; then
		log_skip "ufw が入っていません (ファイアウォールの設定は不要)"
		return 0
	fi
	if status=$(sudo -n ufw status 2>/dev/null); then
		[[ $status == *"Status: active"* ]] && active=1
	elif grep -qE '^[[:space:]]*ENABLED=yes' /etc/ufw/ufw.conf 2>/dev/null; then
		active=1
		status=""
	fi
	if ((!active)); then
		log_skip "ufw は無効です"
		return 0
	fi
	if [[ -z $status ]] && ((!DRY_RUN)); then
		ensure_sudo
		status=$(as_root ufw status 2>/dev/null || true)
	fi
	if grep -qE "^${port}/tcp[[:space:]].*ALLOW" <<<"$status"; then
		log_skip "ufw で ${port}/tcp は許可済み"
		return 0
	fi
	run_root ufw allow "${port}/tcp"
}

# ---------------------------------------------------------------- 手順 11: 検査

step_doctor() {
	step_header 11 "検査 (doctor)"
	local args=() port rc=0 ip
	((SKIP_VLM)) && args+=(--skip-vlm)
	((SKIP_FIRMWARE)) && args+=(--skip-firmware)
	port=$(cfg_get HUB_PORT "$HUB_PORT_DEFAULT")

	if ((DRY_RUN)); then
		printf '[dry-run] %s python -m openmovehub.doctor%s\n' "${UV_RUN[*]}" "${args[*]:+ ${args[*]}}"
		printf '\ndry-run 完了 (何も変更していません)\n'
		return 0
	fi

	if ! command -v uv >/dev/null 2>&1; then
		log_error "uv がありません。bash setup_ubuntu.sh で導入してください。"
		rc=1
	elif ((CHECK_ONLY)) && [[ ! -d $ROOT_DIR/.venv ]]; then
		log_error "Python の環境 (.venv) がありません。bash setup_ubuntu.sh で導入してください。"
		rc=1
	else
		(cd "$ROOT_DIR" && "${UV_RUN[@]}" python -m openmovehub.doctor "${args[@]}") || rc=$?
	fi

	echo
	if ((rc == 0)); then
		echo "SETUP OK"
		echo
		echo "起動方法: bash start_ubuntu.sh"
		echo "ブラウザで開く URL: http://localhost:${port}/"
		echo "ロボット (端末) の設定 GUI に入れる Hub URL:"
		while IFS= read -r ip; do
			[[ -n $ip ]] && echo "  ws://${ip}:${port}/ws/device"
		done < <(lan_ipv4s)
	else
		echo "SETUP INCOMPLETE"
		echo "上の表の対処を参照してください。"
	fi
	if ((NEED_RELOGIN)); then
		echo
		echo "注意: シリアルポートを使うためのグループ (dialout / plugdev) に追加しました。反映には再ログインが必要です。"
	fi
	return "$rc"
}

# ---------------------------------------------------------------- 全体

setup_cleanup() {
	stop_sudo_keepalive
}

setup_main() {
	setup_parse_args "$@"
	cd "$ROOT_DIR" || exit 1

	if ((!DRY_RUN && !CHECK_ONLY)); then
		mkdir -p "$LOG_DIR"
		local log_file
		log_file="$LOG_DIR/setup_$(date +%Y%m%d_%H%M%S).log"
		exec > >(tee -a "$log_file") 2>&1
		log_info "ログ: $log_file"
	fi
	trap setup_cleanup EXIT

	if ((DRY_RUN)); then
		log_info "dry-run: 実行する予定のコマンドを表示します (何も変更しません)。"
	elif ((CHECK_ONLY)); then
		log_info "check: 導入せず検査だけ行います (何も変更しません)。"
	elif ((!ASSUME_YES)) && [[ -t 0 ]]; then
		local answer=""
		read -r -p "約 15 GB をダウンロードします。続けますか [Y/n] " answer || true
		case $answer in
		"" | [Yy] | [Yy][Ee][Ss]) ;;
		*)
			log_info "中止しました。"
			exit 1
			;;
		esac
	fi

	step_preflight
	if ((!CHECK_ONLY)); then
		step_apt
		step_uv
		step_python
		step_config
		step_whisper
		step_voicevox
		step_ollama
		step_platformio
		step_network
	fi
	local rc=0
	step_doctor || rc=$?
	return "$rc"
}
