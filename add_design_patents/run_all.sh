#!/usr/bin/env bash
#
# run_all.sh — IMPACT 不足意匠特許 補完パイプラインを (1)→(2)→(3) の順に連続実行する。
#
# - いずれかのコマンドが失敗 (非ゼロ終了、例: 致命的APIエラーによる sys.exit) したら
#   即座にそこで中断する (set -e)。
# - 正常に完了したステップは state/.done_* マーカーを作成し、次回実行時は自動でスキップする。
#   各 Python スクリプト自体も内部で中断→再開に対応しているため、
#   ステップ途中で中断した場合は再実行すれば続きから進む (このスクリプトはその外側の
#   「ステップ全体が完了したか」だけを管理する)。
# - extract_grant_fulltext.py は ODP バルクダウンロードの (非公開の) 週次クォータを踏まないよう、
#   --sleep-minutes (既定2分/本) のペースで sleep しながら停止せず連続実行し続ける。
#   全アーカイブ完了まで半日〜数日かかるため、このステップの間 run_all.sh はブロックし続ける。
#   途中で中断しても state/ の記録により再実行時は続きから再開する。
#   nohup 等でバックグラウンド実行することを推奨:
#     nohup ./run_all.sh > /mnt/eightthdd/impact/add_design_patent/log/run_all.log 2>&1 &
#
# 使い方:
#   ./run_all.sh              # 通常実行 (完了済みステップはスキップ)
#   ./run_all.sh --force      # 全マーカーを無視して最初から実行
#
# 特定のステップだけやり直したい場合は、対応するマーカーを削除する:
#   rm /mnt/eightthdd/impact/add_design_patent/state/.done_list_missing_patents

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_ACTIVATE="/home/sonozuka/network_fig/venv/bin/activate"
STATE_DIR="/mnt/eightthdd/impact/add_design_patent/state"
mkdir -p "$STATE_DIR"

DONE_1="$STATE_DIR/.done_list_missing_patents"
DONE_2="$STATE_DIR/.done_extract_grant_fulltext"
DONE_3="$STATE_DIR/.done_fetch_citations"

if [[ "${1:-}" == "--force" ]]; then
    echo "⚠️  --force: 完了マーカーを無視して全ステップを再実行します。"
    rm -f "$DONE_1" "$DONE_2" "$DONE_3"
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
echo " IMPACT 不足意匠特許 補完パイプライン"
echo " 開始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "===================================================="

run_step "1/3 対象列挙 (list_missing_patents.py)"        "$DONE_1" python3 list_missing_patents.py
run_step "2/3 全文アーカイブ展開 (extract_grant_fulltext.py)" "$DONE_2" python3 extract_grant_fulltext.py
run_step "3/3 引用取得 (fetch_citations.py)"              "$DONE_3" python3 fetch_citations.py

echo "===================================================="
echo " ✅ 全ステップ完了: $(date '+%Y-%m-%d %H:%M:%S')"
echo "===================================================="
