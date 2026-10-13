#!/usr/bin/env bash
# Ubuntu 22.04 / 24.04 向け 1 クリックセットアップの入口。
#   bash setup_ubuntu.sh            # 導入して検査 (最後に SETUP OK / SETUP INCOMPLETE)
#   bash setup_ubuntu.sh --check    # 検査だけ
#   bash setup_ubuntu.sh --dry-run  # 実行する予定のコマンドを表示するだけ
#   bash setup_ubuntu.sh --help     # オプションの説明
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/setup_ubuntu_lib.sh
source "$SCRIPT_DIR/scripts/setup_ubuntu_lib.sh"

setup_main "$@"
