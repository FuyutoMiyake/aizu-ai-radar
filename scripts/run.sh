#!/bin/zsh
# 週次・会津AI論文ウォッチ実行スクリプト（promax launchd から起動）
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1   # リポジトリルートへ
mkdir -p logs work

STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="logs/run_${STAMP}.log"
RAW="work/raw_output.txt"
LOCKDIR="work/lock.d"
LOCK_TTL=10800  # 3時間

# ---- 二重起動ロック（mkdir方式） ----
acquire_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo $$ > "$LOCKDIR/pid"
    trap 'rm -rf "$LOCKDIR"' EXIT
    return 0
  fi
  local pid age
  pid="$(cat "$LOCKDIR/pid" 2>/dev/null)"
  age=$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || echo 0) ))
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ "$age" -lt "$LOCK_TTL" ]; then
    echo "既に実行中 (pid=$pid, age=${age}s)。終了。"
    exit 0
  fi
  echo "staleロックを奪取 (pid=$pid, age=${age}s)"
  rm -rf "$LOCKDIR"; mkdir "$LOCKDIR"; echo $$ > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR"' EXIT
}
acquire_lock

{
  echo "===== 会津AI論文ウォッチ start: $(date) ====="

  # ローカルMacでテンプレ等を更新した場合の取り込み（失敗しても続行）
  git pull --rebase origin main 2>&1 || echo "WARN: git pull 失敗（続行）"

  # サブスク長期トークン（claude setup-token で発行・設置）
  TOKEN_FILE="secrets/claude_oauth_token.txt"
  if [ -f "$TOKEN_FILE" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE" | tr -d '[:space:]')"
    echo "claude token: loaded from $TOKEN_FILE"
  else
    echo "WARN: $TOKEN_FILE が無い。ローカルログインに依存します。"
  fi

  # ---- 1. HF Daily Papers 取得 ----
  /usr/bin/python3 scripts/fetch_papers.py
  FETCH_RC=$?
  if [ $FETCH_RC -ne 0 ]; then
    echo "fetch_papers.py 失敗 (rc=$FETCH_RC)。エラーメール送信。"
    /usr/bin/python3 scripts/build_and_send.py --error \
      "HuggingFace Daily Papers の取得に失敗しました (rc=$FETCH_RC)。ログ: $LOG"
    exit 1
  fi

  # ---- 2. claude headless で選定・構造化JSON生成 ----
  # プロジェクト固有文脈は git 管理外の secrets/aizu_context.md から結合する
  CONTEXT=""
  if [ -f secrets/aizu_context.md ]; then
    CONTEXT="$(cat secrets/aizu_context.md)"
  else
    echo "WARN: secrets/aizu_context.md が無い。一般文脈でカードを生成します。"
  fi
  PROMPT="$(cat scripts/prompt.md)

$CONTEXT

# 候補論文リスト（この中から選ぶこと）
$(cat work/candidates.json)"

  run_claude() {
    caffeinate -i -s claude -p "$1" \
      --dangerously-skip-permissions > "$RAW" 2> work/claude_err.log
    echo "claude exit code: $?"
    echo "----- raw output (head) -----"; head -c 400 "$RAW"; echo ""
    echo "----- claude stderr (head) -----"; head -c 400 work/claude_err.log; echo ""
  }
  run_claude "$PROMPT"

  # ---- 3. 検証（失敗時は矯正指示つきで1回だけリトライ） ----
  if ! /usr/bin/python3 scripts/build_and_send.py --validate "$RAW"; then
    echo "validate 失敗。矯正指示つきでリトライ。"
    RETRY_PROMPT="$PROMPT

# 追加指示（重要）
前回のあなたの出力は検証に失敗した。説明文・コードフェンスを一切付けず、
仕様どおりのキーを持つJSONオブジェクトのみを出力し直すこと。
arxiv_id は候補リストの値のみ使用。key_points と discussion_questions は必ず3個、runners_up は必ず2個。"
    run_claude "$RETRY_PROMPT"
  fi

  # ---- 4. カード生成 → push → 通知メール（検証NGならここでエラーメール） ----
  /usr/bin/python3 scripts/build_and_send.py "$RAW"
  echo "===== 会津AI論文ウォッチ end: $(date) ====="
} >> "$LOG" 2>&1

# 古いログを30日で掃除
find logs -name "run_*.log" -mtime +30 -delete 2>/dev/null
