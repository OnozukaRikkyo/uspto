#!/usr/bin/env python3
"""
fetch_citations.py — 引用取得 (項目1-(3))

missing_patents.parquet の各意匠特許について、審査中に審査官が引用した先行文献
(審査官引用) を ODP Enriched Citation API v3 から取得する。
引用は全文 XML ではなくこちらから取る (office action の情報を含むため)。

処理:
  Phase A: 特許番号 → 出願番号の解決
           (Patent Search API に 50 件バッチの OR クエリ。fetch_applicants.py と同方式)
  Phase B: 出願番号 50 件を "patentApplicationNumber:(app1 OR app2 OR …)" で
           まとめて enriched_cited_reference_metadata/v3 に照会し (rows=1000 でページング)、
           結果を出願番号ごとに振り分けて examinerCitedReferenceIndicator=true のみ保持。
           (1 特許 1 リクエストの逐次呼び出しを避け、API 呼び出し回数を件数/50 に削減)

出力:
  /mnt/eightthdd/impact/add_design_patent/missing_citations.parquet (1行1引用)
  列: patent_id, application_number, cited_document_id (正規化: US D693,310 S → D693310),
      cited_document_raw, office_action_date, office_action_category,
      citation_category_code, kind_code, npl

検証: office action 識別子 (officeActionDate / officeActionCategory) の欠損率をログに記録。
再開: state/appnum_cache.json と state/citations_processed.txt により途中から再実行可能。

使い方:
  source /home/sonozuka/network_fig/venv/bin/activate
  python3 fetch_citations.py               # 全件
  python3 fetch_citations.py --limit 100   # 動作確認 (100特許のみ)
"""
import argparse
import json
import re
import sys
import time

import pandas as pd
from tqdm import tqdm

from common import (API_BASE, CITATIONS_PARQUET, MISSING_PARQUET, STATE_DIR,
                    api_session, ensure_dirs, load_api_key, request_with_retry,
                    setup_logger, to_api_id)

SEARCH_URL   = f"{API_BASE}/api/v1/patent/applications/search"
ENRICHED_URL = f"{API_BASE}/api/v1/patent/oa/enriched_cited_reference_metadata/v3/records"

APPNUM_CACHE   = STATE_DIR / "appnum_cache.json"          # {patent_id: 出願番号 or ""}
PROCESSED_TXT  = STATE_DIR / "citations_processed.txt"    # 照会済み特許 (0件含む)
CITATIONS_JSONL = STATE_DIR / "citations.jsonl"           # 取得レコード (parquet の元)

BATCH_SIZE = 50    # Phase A/B 共通: OR クエリでまとめる出願件数
ROWS = 1000         # Phase B: 1 バッチ (最大50出願) 分の引用をまかなうページサイズ

logger = setup_logger("fetch_citations_missing")

_TOKEN_RE = re.compile(r'^(D?)(\d+)$')


def normalize_cited_id(raw: str) -> str:
    """'US D693,310 S' → 'D693310' / 'US 20140097797 A1' → '20140097797'。不明は ''。"""
    if not raw:
        return ""
    for token in str(raw).replace(",", "").split():
        m = _TOKEN_RE.match(token.strip().strip("."))
        if m and m.group(2).isdigit():
            return m.group(1) + m.group(2)
    return ""


# ── Phase A: 出願番号の解決 ───────────────────────────────────────────────────

def resolve_app_numbers(session, patent_ids: list[str], sleep: float) -> dict[str, str]:
    cache: dict[str, str] = {}
    if APPNUM_CACHE.exists():
        try:
            cache = json.loads(APPNUM_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    todo = [p for p in patent_ids if p not in cache]
    print(f"🔎 Phase A: 出願番号解決  キャッシュ済み {len(cache):,} / 残り {len(todo):,}")
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]

    for batch in tqdm(batches, desc="出願番号解決", unit="batch", dynamic_ncols=True):
        api_ids = {to_api_id(p): p for p in batch}          # D908314 → D0908314
        q = "applicationMetaData.patentNumber:(" + " OR ".join(api_ids) + ")"
        payload = {"q": q, "pagination": {"offset": 0, "limit": BATCH_SIZE},
                   "fields": ["applicationNumberText", "applicationMetaData.patentNumber"]}
        r = request_with_retry(session, "POST", SEARCH_URL, logger,
                               context=f"appnum batch {batch[0]}…", json=payload, timeout=90)
        if r is None:
            logger.error(f"バッチ失敗 (次回再試行): {batch[0]}…")
            time.sleep(sleep)
            continue
        found = {}
        for it in r.json().get("patentFileWrapperDataBag", []):
            pnum = it.get("applicationMetaData", {}).get("patentNumber", "")
            anum = it.get("applicationNumberText", "")
            if pnum in api_ids and anum:
                found[api_ids[pnum]] = anum
        for p in batch:
            cache[p] = found.get(p, "")
        APPNUM_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        time.sleep(sleep)

    n_missing = sum(1 for p in patent_ids if not cache.get(p))
    if n_missing:
        logger.warning(f"出願番号を解決できなかった特許: {n_missing} 件")
    return cache


# ── Phase B: 審査官引用の取得 (50出願まとめてバッチ照会) ─────────────────────

def fetch_enriched_batch(session, app_nums: list[str]) -> dict[str, list[dict]] | None:
    """
    最大 50 件の出願番号を OR クエリでまとめて照会し、出願番号ごとの
    審査官引用レコード (examinerCitedReferenceIndicator=true) を返す。
    通信失敗時は None (呼び出し側でバッチ全体を次回に再試行)。
    """
    q = "patentApplicationNumber:(" + " OR ".join(app_nums) + ")"
    by_app: dict[str, list[dict]] = {a: [] for a in app_nums}
    start, num_found = 0, None
    while True:
        payload = f"criteria={q}&start={start}&rows={ROWS}"
        r = request_with_retry(
            session, "POST", ENRICHED_URL, logger,
            context=f"enriched batch({len(app_nums)}) {app_nums[0]}… start={start}",
            data=payload, timeout=90,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r is None:
            return None
        resp = r.json().get("response", {})
        if num_found is None:
            num_found = resp.get("numFound", 0)
        docs = resp.get("docs", [])
        for d in docs:
            app = d.get("patentApplicationNumber")
            if app in by_app and d.get("examinerCitedReferenceIndicator") is True:
                by_app[app].append(d)
        start += ROWS
        if start >= num_found or not docs:
            break
    return by_app


def build_parquet():
    if not CITATIONS_JSONL.exists():
        print("引用レコードがまだありません。")
        return
    recs = []
    with open(CITATIONS_JSONL, encoding="utf-8") as f:
        for ln in f:
            try:
                recs.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    if not recs:
        print("引用レコード 0 件。")
        return
    df = pd.DataFrame(recs).drop_duplicates()
    df.to_parquet(CITATIONS_PARQUET, index=False)

    # 検証: office action 識別子の欠損率 (要調査3 の実測)
    n = len(df)
    miss_date = (df["office_action_date"].isna() | (df["office_action_date"] == "")).sum()
    miss_cat  = (df["office_action_category"].isna() | (df["office_action_category"] == "")).sum()
    miss_cited = (df["cited_document_id"] == "").sum()
    msg = (f"citations={n:,} patents={df['patent_id'].nunique():,} | "
           f"officeActionDate 欠損 {miss_date} ({100*miss_date/n:.2f}%) | "
           f"officeActionCategory 欠損 {miss_cat} ({100*miss_cat/n:.2f}%) | "
           f"被引用番号 正規化不能 {miss_cited} ({100*miss_cited/n:.2f}%)")
    print(f"📊 {msg}")
    logger.info(msg)
    print(f"✅ 保存完了: {CITATIONS_PARQUET}")


def main():
    ap = argparse.ArgumentParser(description="不足意匠特許の審査官引用を取得")
    ap.add_argument("--sleep", type=float, default=0.5, help="リクエスト間の待機秒数")
    ap.add_argument("--limit", type=int, default=0, help="処理する特許数の上限 (0=無制限)")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="API を叩かず jsonl から parquet を再構築するのみ")
    args = ap.parse_args()

    ensure_dirs()
    if args.rebuild_only:
        build_parquet()
        return

    api_key = load_api_key()
    session = api_session(api_key)

    if not MISSING_PARQUET.exists():
        sys.exit(f"{MISSING_PARQUET} がありません。先に list_missing_patents.py を実行してください。")
    missing = pd.read_parquet(MISSING_PARQUET)
    patent_ids = missing["patent_id"].tolist()
    if args.limit:
        patent_ids = patent_ids[:args.limit]

    # ── Phase A ──
    appnums = resolve_app_numbers(session, patent_ids, args.sleep)

    # ── Phase B (50出願/バッチで一括照会) ──
    processed = set()
    if PROCESSED_TXT.exists():
        processed = {ln.strip() for ln in PROCESSED_TXT.read_text(encoding="utf-8").splitlines()}
    todo = [p for p in patent_ids if p not in processed and appnums.get(p)]
    skipped_noapp = sum(1 for p in patent_ids if not appnums.get(p))
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    print(f"📡 Phase B: 引用取得  処理済み {len(processed):,} / 残り {len(todo):,} "
          f"({len(batches):,} バッチ、出願番号なしスキップ {skipped_noapp:,})")

    n_with_cit = 0
    for batch in tqdm(batches, desc="引用取得", unit="batch", dynamic_ncols=True):
        app_to_pid = {appnums[p]: p for p in batch}   # 出願番号 → 特許ID (1出願1特許)
        by_app = fetch_enriched_batch(session, list(app_to_pid))
        time.sleep(args.sleep)
        if by_app is None:
            tqdm.write(f"  [ERROR] バッチ失敗 ({batch[0]}…): 次回再試行します。")
            continue

        batch_hit = 0
        with open(CITATIONS_JSONL, "a", encoding="utf-8") as f:
            for app_num, docs in by_app.items():
                pid = app_to_pid[app_num]
                if docs:
                    batch_hit += 1
                    for d in docs:
                        rec = {
                            "patent_id": pid,
                            "application_number": app_num,
                            "cited_document_id": normalize_cited_id(d.get("citedDocumentIdentifier", "")),
                            "cited_document_raw": d.get("citedDocumentIdentifier", "") or "",
                            "office_action_date": (d.get("officeActionDate") or "")[:10],
                            "office_action_category": d.get("officeActionCategory", "") or "",
                            "citation_category_code": d.get("citationCategoryCode", "") or "",
                            "kind_code": d.get("kindCode", "") or "",
                            "npl": bool(d.get("nplIndicator", False)),
                        }
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_with_cit += batch_hit
        with open(PROCESSED_TXT, "a", encoding="utf-8") as f:
            for pid in batch:
                f.write(pid + "\n")
        tqdm.write(f"  📄 バッチ {len(batch)}件中 引用あり {batch_hit}件")

    print(f"\n今回実行で引用が見つかった特許: {n_with_cit:,} 件")
    build_parquet()
    print("\n✅ 完了")


if __name__ == "__main__":
    main()
