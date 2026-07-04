#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HuggingFace Daily Papers API から過去7日分の論文を取得し、
既出（docs/cards.json 掲載済み）を除いた upvote 上位20本を
work/candidates.json に書き出す。stdlib のみ使用。

exit code: 0=正常 / 2=全日付失敗またはプール5本未満（エラーメール経路へ）
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS_JSON = os.path.join(BASE, "docs", "cards.json")
OUT_PATH = os.path.join(BASE, "work", "candidates.json")

API = "https://huggingface.co/api/daily_papers?date={date}&limit=50"
UA = "aizu-ai-radar/1.0 (weekly paper digest; github.com/FuyutoMiyake/aizu-ai-radar)"
JST = timezone(timedelta(hours=9))

DAYS = 7          # 遡る日数
POOL_SIZE = 20    # 候補プール本数
MIN_POOL = 5      # これ未満なら異常扱い
ABSTRACT_MAX = 1200


def fetch_day(date_str):
    """1日分を取得。5s/15sバックオフで2回リトライ。失敗は None、空日は []。"""
    url = API.format(date=date_str)
    for i, wait in enumerate([0, 5, 15]):
        if wait:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            print(f"[WARN] {date_str} 取得失敗 (try {i + 1}): {e}", file=sys.stderr)
    return None


def upvotes_of(item):
    """paper.upvotes / numUpvotes の両対応で upvote 数を返す。"""
    paper = item.get("paper") or {}
    for v in (paper.get("upvotes"), item.get("numUpvotes")):
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def normalize(item, date_str):
    paper = item.get("paper") or {}
    arxiv_id = (paper.get("id") or "").strip()
    if not arxiv_id or not paper.get("title"):
        return None
    authors = [a.get("name", "") for a in (paper.get("authors") or []) if a.get("name")]
    author_str = ", ".join(authors[:3]) + (", et al." if len(authors) > 3 else "")
    summary = (paper.get("summary") or "").strip().replace("\n", " ")
    if len(summary) > ABSTRACT_MAX:
        summary = summary[:ABSTRACT_MAX] + "…"
    return {
        "arxiv_id": arxiv_id,
        "title": paper.get("title", "").strip(),
        "summary": summary,
        "authors": author_str,
        "upvotes": upvotes_of(item),
        "published_at": item.get("publishedAt") or "",
        "date": date_str,
        "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
        "hf_url": f"https://huggingface.co/papers/{arxiv_id}",
    }


def load_featured_ids():
    """cards.json に既出の arxiv_id（featured + runners_up）を集める。"""
    if not os.path.exists(CARDS_JSON):
        return set()
    try:
        with open(CARDS_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[WARN] cards.json 読込失敗（既出除外なしで続行）: {e}", file=sys.stderr)
        return set()
    ids = set()
    for card in data.get("cards", []):
        fid = (card.get("featured") or {}).get("arxiv_id")
        if fid:
            ids.add(fid)
        for ru in card.get("runners_up", []):
            if ru.get("arxiv_id"):
                ids.add(ru["arxiv_id"])
    return ids


def main():
    now = datetime.now(timezone.utc)
    dates = [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(1, DAYS + 1)]

    papers = {}
    ok_days = 0
    for ds in dates:
        items = fetch_day(ds)
        if items is None:
            continue
        ok_days += 1
        for item in items:
            p = normalize(item, ds)
            if not p:
                continue
            prev = papers.get(p["arxiv_id"])
            if prev is None or p["upvotes"] > prev["upvotes"]:
                papers[p["arxiv_id"]] = p

    if ok_days == 0:
        print("[ERROR] 全日付の取得に失敗", file=sys.stderr)
        return 2

    seen = load_featured_ids()
    pool = [p for p in papers.values() if p["arxiv_id"] not in seen]
    pool.sort(key=lambda p: p["upvotes"], reverse=True)
    pool = pool[:POOL_SIZE]

    if len(pool) < MIN_POOL:
        print(f"[ERROR] 候補プール不足: {len(pool)}本 (< {MIN_POOL})", file=sys.stderr)
        return 2

    out = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "week_of": datetime.now(JST).strftime("%Y-%m-%d"),
        "pool_size": len(pool),
        "candidates": pool,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[OK] 候補 {len(pool)}本 → {OUT_PATH}（{ok_days}/{DAYS}日分取得, 既出除外 {len(seen)}件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
