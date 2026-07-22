#!/usr/bin/env bash
#
# run_validation_and_integration.sh — 項目1.1・1.2 を連続実行
#
# 項目1の3つのプログラムが出力したデータの品質チェック(1.1)と
# IMPACT マスタへの統合(1.2)を順序実行する。
#
# 使い方:
#   ./run_validation_and_integration.sh              # 通常実行
#   ./run_validation_and_integration.sh --force      # マーカーを無視して再実行
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_ACTIVATE="/home/sonozuka/network_fig/venv/bin/activate"
STATE_DIR="/mnt/eightthdd/impact/add_design_patent/state"
mkdir -p "$STATE_DIR"

DONE_1="$STATE_DIR/.done_validate_and_consolidate"
DONE_2="$STATE_DIR/.done_integrate_to_impact"

if [[ "${1:-}" == "--force" ]]; then
    echo "⚠️  --force: マーカーを無視して全ステップを再実行します。"
    rm -f "$DONE_1" "$DONE_2"
fi

# shellcheck disable=SC1090
source "$VENV_ACTIVATE"

run_step() {
    local label="$1" marker="$2"
    shift 2
    if [[ -f "$marker" ]]; then
        echo "==> [$label] 完了済み (マーカー: $marker) → スキップ"
        return 0
    fi
    echo "==> [$label] 開始: $*"
    "$@"
    touch "$marker"
    echo "==> [$label] 完了 (マーカー作成: $marker)"
}

echo "===================================================="
echo " 項目1.1・1.2: 検証と統合"
echo " 開始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "===================================================="

run_step "1.1 取得データの統合・検証 (validate_and_consolidate.py)" "$DONE_1" python3 validate_and_consolidate.py
run_step "1.2 IMPACT データベースへの統合 (integrate_to_impact.py)" "$DONE_2" python3 integrate_to_impact.py

echo "===================================================="
echo " ✅ 全ステップ完了: $(date '+%Y-%m-%d %H:%M:%S')"
echo "===================================================="
