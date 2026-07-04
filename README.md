# Aizu AI Radar（会津AI論文ウォッチ）

毎週月曜朝、HuggingFace Daily Papers の上位論文から「会津・都市AI OS」文脈で1本を選び、
日本語インフォグラフィックカードを自動生成して GitHub Pages に公開する週次ジョブ。

公開ページ: https://fuyutomiyake.github.io/aizu-ai-radar/

## 仕組み

```
launchd (月曜 9:00 JST)
 → scripts/run.sh
    1. fetch_papers.py   HF Daily Papers API 7日分 → 候補20本 (work/candidates.json)
    2. claude -p         候補から3本選定・構造化JSON出力（サブスク範囲・ツール不使用）
    3. build_and_send.py JSON検証 → HTMLカード生成 → index再生成 → git push → 通知メール
```

- LLM の責務は構造化 JSON を出すだけ。HTML はテンプレ（`scripts/card_template.html`）から決定的に生成
- 既出管理は `docs/cards.json`（マニフェスト兼状態ファイル。push で自動バックアップ）
- 同一週の再実行はカード上書き＝冪等

## 選定基準

6トラック（AI Agents / Voice AI・Realtime / Human-AI collaboration / Eval・Safety /
Healthcare・Elderly・Mobility / Local LLM・Edge・日本語AI）×
HF upvotes × 実装しやすさ × デモ化可能性。「会津で2週間以内に試せるか」を強く加点。

## セットアップ（実行マシン）

1. このリポジトリを clone（実行マシンでは書き込み権付き deploy key を使用）
2. `secrets/`（git 管理外）に以下4ファイルを配置:
   - `claude_oauth_token.txt` — `claude setup-token` で発行した長期トークン（chmod 600）
   - `gmail_app_password.txt` — Gmail アプリパスワード16桁（chmod 600）
   - `mail_config.json` — `{"sender": "...", "recipients": ["..."]}`
   - `aizu_context.md` — プロジェクト固有文脈（プロンプトに結合される。無ければ一般文脈で生成）
3. launchd: `scripts/com.fm.aizu-ai-radar.plist` を `~/Library/LaunchAgents/` に置いて bootstrap
4. スリープ起床: `sudo pmset repeat wakeorpoweron MT 08:55:00`
   （pmset repeat は1本しか持てないため、他ジョブの起床曜日と合わせて1コマンドで設定すること）

## 手動実行

```sh
scripts/run.sh                          # フル実行
python3 scripts/fetch_papers.py         # 候補取得のみ
python3 scripts/build_and_send.py work/raw_output.txt --dry-run   # push・メールなし
```
