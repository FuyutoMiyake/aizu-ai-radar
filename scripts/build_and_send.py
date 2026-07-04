#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude の構造化JSON出力を検証し、HTMLカード生成 → index再生成 →
git push → 通知メール（Gmail SMTP）を行う。stdlib のみ使用。

使い方:
  build_and_send.py work/raw_output.txt              本処理
  build_and_send.py --validate work/raw_output.txt   検証のみ (rc 0/1)
  build_and_send.py work/raw_output.txt --dry-run    push・メールをスキップ
  build_and_send.py --preview IN.json OUT.html       カード描画のみ（状態を触らない）
  build_and_send.py --error "メッセージ"              エラーメール送信
"""
import html
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(BASE, "docs")
CARDS_DIR = os.path.join(DOCS, "cards")
CARDS_JSON = os.path.join(DOCS, "cards.json")
CANDIDATES = os.path.join(BASE, "work", "candidates.json")
CARD_TMPL = os.path.join(BASE, "scripts", "card_template.html")
INDEX_TMPL = os.path.join(BASE, "scripts", "index_template.html")
PW_PATH = os.path.join(BASE, "secrets", "gmail_app_password.txt")
MAIL_CFG_PATH = os.path.join(BASE, "secrets", "mail_config.json")


def _load_mail_cfg():
    """送信元・宛先は secrets/mail_config.json から読む（公開リポジトリに載せない）"""
    try:
        with open(MAIL_CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_MAIL_CFG = _load_mail_cfg()
SENDER = _MAIL_CFG.get("sender", "")
RECIPIENTS = _MAIL_CFG.get("recipients", [])
MAIL_NAME = "会津AI論文ウォッチ"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
PAGES_URL = "https://fuyutomiyake.github.io/aizu-ai-radar"
JST = timezone(timedelta(hours=9))

DIAGRAM_TYPES = ("flow", "comparison", "bullets")


def now_jst():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


# ---------------- パース・検証 ----------------

def parse_json(raw):
    """フェンス除去 + 最初の{〜最後の} 抽出（補助金ウォッチ parse_results と同型）"""
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if m:
        raw = m.group(1)
    else:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            raw = raw[s:e + 1]
    return json.loads(raw)


def load_candidates():
    if not os.path.exists(CANDIDATES):
        return {}
    with open(CANDIDATES, encoding="utf-8") as f:
        data = json.load(f)
    return {c["arxiv_id"]: c for c in data.get("candidates", [])}


def validate(data, cand_by_id):
    """検証エラーのリストを返す（空なら合格）"""
    errs = []
    feat = data.get("featured")
    if not isinstance(feat, dict):
        return ["featured がオブジェクトではない"]

    for key in ("arxiv_id", "title_ja", "title_en", "one_liner"):
        if not (feat.get(key) or "").strip():
            errs.append(f"featured.{key} が空")

    if cand_by_id and feat.get("arxiv_id") not in cand_by_id:
        errs.append(f"featured.arxiv_id '{feat.get('arxiv_id')}' が候補リストに存在しない（捏造の疑い）")

    kp = feat.get("key_points")
    if not isinstance(kp, list) or len(kp) != 3:
        errs.append("key_points が3個ではない")

    dq = feat.get("discussion_questions")
    if not isinstance(dq, list) or len(dq) != 3:
        errs.append("discussion_questions が3個ではない")

    dg = feat.get("diagram") or {}
    dtype = dg.get("type")
    if dtype not in DIAGRAM_TYPES:
        errs.append(f"diagram.type '{dtype}' が不正（flow/comparison/bullets のいずれか）")
    elif dtype == "flow" and not (isinstance(dg.get("steps"), list) and len(dg["steps"]) >= 2):
        errs.append("diagram(flow).steps が2個未満")
    elif dtype == "comparison" and not (isinstance(dg.get("left"), dict) and isinstance(dg.get("right"), dict)):
        errs.append("diagram(comparison).left/right が不足")
    elif dtype == "bullets" and not (isinstance(dg.get("items"), list) and len(dg["items"]) >= 2):
        errs.append("diagram(bullets).items が2個未満")

    ac = feat.get("aizu_connection") or {}
    if not (ac.get("summary") or "").strip():
        errs.append("aizu_connection.summary が空")
    if not (ac.get("try_in_2_weeks") or "").strip():
        errs.append("aizu_connection.try_in_2_weeks が空")

    ru = data.get("runners_up")
    if not isinstance(ru, list) or len(ru) != 2:
        errs.append("runners_up が2個ではない")
    else:
        for i, r in enumerate(ru):
            if not (r.get("title_ja") or "").strip():
                errs.append(f"runners_up[{i}].title_ja が空")
            if cand_by_id and r.get("arxiv_id") not in cand_by_id:
                errs.append(f"runners_up[{i}].arxiv_id が候補リストに存在しない")
    return errs


# ---------------- カード描画 ----------------

def render_flow(dg):
    parts = ['<div class="flow">']
    steps = dg.get("steps", [])
    for i, st in enumerate(steps):
        if i:
            parts.append('<div class="flow-conn"><span></span></div>')
        parts.append(
            f'<div class="flow-step"><div class="flow-no">{i + 1}</div>'
            f'<div class="flow-body"><b>{esc(st.get("label"))}</b>'
            f'<p>{esc(st.get("desc"))}</p></div></div>')
    parts.append("</div>")
    return "".join(parts)


def render_comparison(dg):
    def col(side, cls):
        items = "".join(f"<li>{esc(x)}</li>" for x in side.get("items", []))
        return (f'<div class="comp-col {cls}"><h4>{esc(side.get("title"))}</h4>'
                f"<ul>{items}</ul></div>")
    return ('<div class="compare">'
            + col(dg.get("left", {}), "left")
            + col(dg.get("right", {}), "right")
            + "</div>")


def render_bullets(dg):
    rows = "".join(
        f'<div class="dbullet"><b>{esc(it.get("label"))}</b><p>{esc(it.get("desc"))}</p></div>'
        for it in dg.get("items", []))
    return f'<div class="dbullets">{rows}</div>'


def render_diagram(dg):
    return {"flow": render_flow, "comparison": render_comparison,
            "bullets": render_bullets}[dg["type"]](dg)


def build_card_html(data, cand):
    feat = data["featured"]
    ac = feat.get("aizu_connection") or {}
    dg = feat.get("diagram") or {}
    links = feat.get("links") or {}
    arxiv_id = feat.get("arxiv_id", "")

    # 事実系フィールドは候補リスト（HF API由来）を正とする
    upvotes = cand.get("upvotes", feat.get("upvotes", 0))
    authors = cand.get("authors", "")
    arxiv_url = cand.get("arxiv_url") or links.get("arxiv") or f"https://arxiv.org/abs/{arxiv_id}"
    hf_url = cand.get("hf_url") or links.get("hf") or f"https://huggingface.co/papers/{arxiv_id}"

    points = "".join(
        f'<div class="point"><b class="no">{i + 1}</b><p>{esc(p)}</p></div>'
        for i, p in enumerate(feat["key_points"]))
    questions = "".join(f'<div class="q">{esc(q)}</div>'
                        for q in feat["discussion_questions"])
    tracks = "".join(f'<span class="track">{esc(t)}</span>'
                     for t in (ac.get("tracks") or []))

    runners = []
    for r in data.get("runners_up", []):
        rid = r.get("arxiv_id", "")
        rurl = r.get("arxiv") or f"https://arxiv.org/abs/{rid}"
        up = f'<span class="r-up">▲ {int(r["upvotes"])}</span>' if isinstance(r.get("upvotes"), (int, float)) else ""
        runners.append(
            f'<div class="runner"><div class="r-title"><a href="{esc(rurl)}">'
            f'{esc(r.get("title_ja"))}</a>{up}</div>'
            f'<div class="r-en">{esc(r.get("title_en"))}</div>'
            f'<div class="r-comment">{esc(r.get("comment"))}</div></div>')

    with open(CARD_TMPL, encoding="utf-8") as f:
        tmpl = f.read()
    repl = {
        "{{WEEK_OF}}": esc(data.get("week_of", "")),
        "{{TITLE_JA}}": esc(feat.get("title_ja")),
        "{{TITLE_EN}}": esc(feat.get("title_en")),
        "{{ONE_LINER}}": esc(feat.get("one_liner")),
        "{{KEY_POINTS_HTML}}": points,
        "{{DIAGRAM_TITLE}}": esc(dg.get("title", "")),
        "{{DIAGRAM_HTML}}": render_diagram(dg),
        "{{AIZU_SUMMARY}}": esc(ac.get("summary")),
        "{{TRY_2W}}": esc(ac.get("try_in_2_weeks")),
        "{{TRACKS_HTML}}": tracks,
        "{{QUESTIONS_HTML}}": questions,
        "{{ARXIV_URL}}": esc(arxiv_url),
        "{{HF_URL}}": esc(hf_url),
        "{{UPVOTES}}": esc(upvotes),
        "{{AUTHORS}}": esc(authors),
        "{{ARXIV_ID}}": esc(arxiv_id),
        "{{RUNNERS_UP_HTML}}": "".join(runners),
        "{{GENERATED_AT}}": esc(now_jst()),
    }
    for k, v in repl.items():
        tmpl = tmpl.replace(k, v)
    return tmpl


# ---------------- cards.json / index ----------------

def load_cards():
    if os.path.exists(CARDS_JSON):
        try:
            with open(CARDS_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cards": []}


def update_cards_json(data, cand):
    feat = data["featured"]
    entry = {
        "week_of": data.get("week_of", ""),
        "featured": {
            "arxiv_id": feat.get("arxiv_id"),
            "title_ja": feat.get("title_ja"),
            "title_en": feat.get("title_en"),
            "upvotes": cand.get("upvotes", 0),
        },
        "runners_up": [
            {"arxiv_id": r.get("arxiv_id"), "title_ja": r.get("title_ja")}
            for r in data.get("runners_up", [])
        ],
        "card": f"cards/{data.get('week_of', '')}.html",
        "generated_at": now_jst(),
    }
    manifest = load_cards()
    cards = [c for c in manifest.get("cards", []) if c.get("week_of") != entry["week_of"]]
    cards.append(entry)
    cards.sort(key=lambda c: c.get("week_of", ""), reverse=True)
    manifest["cards"] = cards
    with open(CARDS_JSON, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def build_index(manifest):
    cards = manifest.get("cards", [])
    if cards:
        latest = cards[0]
        hero = (
            '<div class="latest-label">LATEST — 今週の1本</div>'
            f'<a class="hero" href="{esc(latest["card"])}">'
            f'<span class="week">{esc(latest["week_of"])} 週</span>'
            f'<h2>{esc(latest["featured"]["title_ja"])}</h2>'
            f'<div class="en">{esc(latest["featured"]["title_en"])}</div>'
            f'<div class="up">▲ {esc(latest["featured"]["upvotes"])} upvotes — カードを読む →</div></a>')
        rows = "".join(
            f'<a class="item" href="{esc(c["card"])}"><span class="w">{esc(c["week_of"])}</span>'
            f'<span class="t">{esc(c["featured"]["title_ja"])}</span>'
            f'<span class="u">▲ {esc(c["featured"]["upvotes"])}</span></a>'
            for c in cards)
    else:
        hero = ""
        rows = '<div class="empty">まだカードがありません。最初の月曜をお待ちください。</div>'

    with open(INDEX_TMPL, encoding="utf-8") as f:
        tmpl = f.read()
    tmpl = (tmpl.replace("{{HERO_HTML}}", hero)
                .replace("{{LIST_HTML}}", rows)
                .replace("{{COUNT}}", str(len(cards)))
                .replace("{{UPDATED_AT}}", now_jst()))
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(tmpl)


# ---------------- git / メール ----------------

def git_push(week_of, title_ja):
    def run(*args):
        return subprocess.run(["git", "-C", BASE] + list(args),
                              capture_output=True, text=True, timeout=120)
    run("add", "docs")
    st = run("status", "--porcelain", "docs")
    if not st.stdout.strip():
        print("[INFO] docs/ に変更なし。commit スキップ")
    else:
        c = run("commit", "-m", f"card: {week_of} {title_ja}")
        if c.returncode != 0:
            print(f"[WARN] commit 失敗: {c.stderr[:300]}", file=sys.stderr)
            return False
    p = run("push", "origin", "main")
    if p.returncode != 0:
        print(f"[WARN] push 失敗: {p.stderr[:300]}", file=sys.stderr)
        return False
    print("[OK] git push 完了")
    return True


def get_app_password():
    env = os.environ.get("GMAIL_APP_PASSWORD")
    if env:
        return env.strip().replace(" ", "")
    if os.path.exists(PW_PATH):
        with open(PW_PATH, encoding="utf-8") as f:
            return f.read().strip().replace(" ", "")
    return None


def send_mail(subject, body_html):
    if not SENDER or not RECIPIENTS:
        print(f"[ERROR] {MAIL_CFG_PATH} が未設定（sender/recipients）。送信スキップ。", file=sys.stderr)
        return False
    pw = get_app_password()
    if not pw:
        print("[ERROR] Gmailアプリパスワード未設定。送信スキップ。", file=sys.stderr)
        return False
    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(MAIL_NAME, "utf-8")), SENDER))
    msg["To"] = ", ".join(RECIPIENTS)
    ctx = ssl.create_default_context()
    for attempt in (1, 2):
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=120) as s:
                s.starttls(context=ctx)
                s.login(SENDER, pw)
                s.sendmail(SENDER, RECIPIENTS, msg.as_string())
            print(f"[OK] メール送信完了: {subject}")
            return True
        except Exception as e:
            print(f"[WARN] SMTP失敗 (try {attempt}): {e}", file=sys.stderr)
            if attempt == 1:
                time.sleep(60)
    return False


MAIL_STYLE = "font-family:-apple-system,'Hiragino Sans',sans-serif;max-width:640px;margin:0 auto;color:#1a2233;"


def notify_html(data, cand, push_ok):
    feat = data["featured"]
    card_url = f"{PAGES_URL}/cards/{data.get('week_of')}.html"
    points = "".join(f'<li style="margin-bottom:6px;">{esc(p)}</li>' for p in feat["key_points"])
    runners = "".join(
        f'<div style="font-size:13px;color:#5a6478;margin-bottom:6px;">・{esc(r.get("title_ja"))}'
        f'<span style="color:#98a1b3;"> — {esc(r.get("comment"))}</span></div>'
        for r in data.get("runners_up", []))
    push_note = "" if push_ok else (
        '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:10px 14px;'
        'margin:14px 0;font-size:13px;color:#7f1d1d;">⚠️ GitHub への push に失敗しました。'
        'カードはローカル commit 済みで、次回実行時の push で自動回復します。'
        '手動なら: <code>ssh promax "cd ~/projects/aizu-ai-radar && git push origin main"</code></div>')
    return f"""<div style="{MAIL_STYLE}">
    <h2 style="font-size:17px;border-bottom:3px solid #274690;padding-bottom:8px;">
      📡 会津AI論文ウォッチ <span style="font-size:12px;color:#98a1b3;font-weight:400;">{esc(data.get('week_of'))} 週</span></h2>
    <div style="background:#eef1f9;border-left:4px solid #274690;padding:14px 18px;border-radius:0 8px 8px 0;margin:16px 0;">
      <div style="font-size:16px;font-weight:700;line-height:1.6;">{esc(feat.get('title_ja'))}</div>
      <div style="font-size:12px;color:#5a6478;margin-top:4px;">{esc(feat.get('title_en'))} ·
        <span style="color:#b98a2e;font-weight:700;">▲ {esc(cand.get('upvotes', ''))}</span></div>
    </div>
    <p style="font-size:14px;font-weight:700;">{esc(feat.get('one_liner'))}</p>
    <ul style="font-size:13.5px;line-height:1.7;padding-left:20px;">{points}</ul>
    <p style="margin:18px 0;"><a href="{esc(card_url)}" style="background:#274690;color:#fff;text-decoration:none;
      font-size:14px;font-weight:700;padding:10px 22px;border-radius:8px;display:inline-block;">
      カードを読む（図解つき）</a><br>
      <span style="font-size:11px;color:#98a1b3;">※ Pages 反映まで1〜2分かかることがあります</span></p>
    {push_note}
    <div style="font-size:12px;font-weight:700;color:#5a6478;margin-bottom:6px;">今週の選外</div>
    {runners}
    <p style="font-size:12.5px;color:#5a6478;margin-top:14px;">{esc(data.get('selection_note'))}</p>
    <p style="font-size:11px;color:#98a1b3;margin-top:22px;border-top:1px solid #e3e6ec;padding-top:10px;">
      promax launchd 週次ジョブ / <a href="{PAGES_URL}/" style="color:#274690;">アーカイブ</a> /
      AI自動生成につき要点は原論文でご確認ください。</p></div>"""


def error_html(msg):
    return f"""<div style="{MAIL_STYLE}">
    <h2 style="font-size:17px;border-bottom:3px solid #274690;padding-bottom:8px;">📡 会津AI論文ウォッチ</h2>
    <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:14px;margin-top:12px;">
      <b style="color:#dc2626;">⚠️ 今週は正常に生成できませんでした</b>
      <pre style="font-size:12px;color:#7f1d1d;white-space:pre-wrap;margin:8px 0 0;">{esc(msg)}</pre>
    </div>
    <p style="font-size:12px;color:#5a6478;margin-top:12px;">トークン切れの場合は、ローカルMacで
    <code>claude setup-token</code> を再発行し、promax の
    <code>~/projects/aizu-ai-radar/secrets/claude_oauth_token.txt</code> を更新してください。</p></div>"""


# ---------------- main ----------------

def main():
    argv = sys.argv[1:]

    if argv and argv[0] == "--error":
        msg = argv[1] if len(argv) > 1 else "原因不明のエラー"
        ok = send_mail(f"⚠️ 会津AI論文ウォッチ: 生成失敗 {datetime.now(JST).strftime('%Y-%m-%d')}",
                       error_html(msg))
        return 0 if ok else 1

    if argv and argv[0] == "--preview":
        with open(argv[1], encoding="utf-8") as f:
            data = parse_json(f.read())
        cand = load_candidates().get(data["featured"].get("arxiv_id"), {})
        out = argv[2] if len(argv) > 2 else "preview.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(build_card_html(data, cand))
        print(f"[OK] preview → {out}")
        return 0

    validate_only = argv and argv[0] == "--validate"
    if validate_only:
        argv = argv[1:]
    if not argv:
        print("usage: build_and_send.py [--validate|--preview|--error] FILE [--dry-run]", file=sys.stderr)
        return 1
    raw_path = argv[0]
    dry_run = "--dry-run" in argv

    with open(raw_path, encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        if validate_only:
            print("[NG] 出力が空", file=sys.stderr)
            return 1
        send_mail(f"⚠️ 会津AI論文ウォッチ: 生成失敗 {datetime.now(JST).strftime('%Y-%m-%d')}",
                  error_html("claude の出力が空でした（トークン切れ or 実行失敗の可能性）。"))
        return 1

    cand_by_id = load_candidates()
    try:
        data = parse_json(raw)
        errs = validate(data, cand_by_id)
    except Exception as e:
        errs = [f"JSONパース失敗: {e}"]
        data = None

    if validate_only:
        if errs:
            print("[NG] " + " / ".join(errs), file=sys.stderr)
            return 1
        print("[OK] validate 合格")
        return 0

    if errs:
        snippet = raw[:1500]
        send_mail(f"⚠️ 会津AI論文ウォッチ: 解析失敗 {datetime.now(JST).strftime('%Y-%m-%d')}",
                  error_html("検証エラー:\n- " + "\n- ".join(errs) + f"\n\n--- 生出力(先頭) ---\n{snippet}"))
        return 1

    # カード生成
    week_of = data.get("week_of") or datetime.now(JST).strftime("%Y-%m-%d")
    data["week_of"] = week_of
    cand = cand_by_id.get(data["featured"]["arxiv_id"], {})
    os.makedirs(CARDS_DIR, exist_ok=True)
    card_path = os.path.join(CARDS_DIR, f"{week_of}.html")
    with open(card_path, "w", encoding="utf-8") as f:
        f.write(build_card_html(data, cand))
    print(f"[OK] カード生成: {card_path}")

    manifest = update_cards_json(data, cand)
    build_index(manifest)
    print("[OK] cards.json / index.html 更新")

    if dry_run:
        print("[DRY-RUN] push・メールはスキップ")
        return 0

    push_ok = git_push(week_of, data["featured"].get("title_ja", ""))
    subject = f"📡 会津AI論文ウォッチ {week_of}: {data['featured'].get('title_ja', '')}"
    if not push_ok:
        subject += "（公開待ち）"
    send_mail(subject, notify_html(data, cand, push_ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
