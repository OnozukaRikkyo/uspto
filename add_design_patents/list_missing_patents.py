#!/usr/bin/env python3
"""
list_missing_patents.py — 対象列挙 (項目1-(1))

2007-01-01 以降に登録された全意匠特許のうち、IMPACT マスタ
(/mnt/eightthdd/uspto/data/*.csv) に存在しないものを列挙する。

入力:
  1. ODP バルク製品 PVGPATDIS の g_patent.tsv.zip (特許一括テーブル。自動ダウンロード)
     ※テーブルは四半期更新のため、最終登録日以降の分は ODP Patent Search API で補完する
  2. IMPACT マスタの特許番号一覧 (/mnt/eightthdd/uspto/data/*.csv の id 列)

出力:
  /mnt/eightthdd/impact/add_design_patent/missing_patents.parquet
  (列: patent_id [D+7桁ゼロ埋め], grant_date, source)

使い方:
  source /home/sonozuka/network_fig/venv/bin/activate
  python3 list_missing_patents.py                # 通常実行
  python3 list_missing_patents.py --refresh-bulk # g_patent を再ダウンロード
  python3 list_missing_patents.py --no-api-supplement
"""
import argparse
import sys
import time
from datetime import date

import pandas as pd
from tqdm import tqdm

from common import (API_BASE, BULK_DIR, IMPACT_ID_RE, MASTER_DATA_DIR,
                    MISSING_PARQUET, api_session, download_file, ensure_dirs,
                    id_to_int, load_api_key, request_with_retry, setup_logger,
                    to_impact_id)

G_PATENT_URL = f"{API_BASE}/api/v1/datasets/products/files/PVGPATDIS/g_patent.tsv.zip"
G_PATENT_ZIP = BULK_DIR / "g_patent.tsv.zip"
CUTOFF_DATE = "2007-01-01"
PAGE_SIZE = 100

logger = setup_logger("list_missing_patents")


# ── ① 一括テーブルから意匠特許を抽出 ──────────────────────────────────────────

def load_design_grants_from_bulk() -> pd.DataFrame:
    """
    g_patent.tsv.zip から意匠特許を抽出する。
    withdrawn=1 (後日撤回された特許) は除外する — ODP の Patent Search API・
    実際の PTGRDT 週次アーカイブのいずれにも存在せず、取得しても必ず「未発見」に
    なることを実測で確認済み (doc/障害記録_20260720_withdrawn特許.md 参照)。
    """
    print("📄 g_patent.tsv.zip を読み込み中 (数分かかります)...")
    df = pd.read_csv(G_PATENT_ZIP, sep="\t",
                     usecols=["patent_id", "patent_type", "patent_date", "withdrawn"],
                     dtype=str, compression="zip")
    n_total = len(df)
    is_design = df["patent_type"] == "design"
    is_recent = df["patent_date"] >= CUTOFF_DATE
    is_withdrawn = df["withdrawn"] == "1"

    n_withdrawn = (is_design & is_recent & is_withdrawn).sum()
    df = df[is_design & is_recent & ~is_withdrawn]
    print(f"   全特許 {n_total:,} 行 → 意匠 & {CUTOFF_DATE} 以降: {len(df) + n_withdrawn:,} 件 "
         f"(うち withdrawn 除外: {n_withdrawn:,} 件) → 対象: {len(df):,} 件")
    return df[["patent_id", "patent_date"]].rename(columns={"patent_date": "grant_date"})


# ── API 補完 (一括テーブル最終日以降の登録分) ────────────────────────────────

def supplement_from_api(session, after_date: str) -> pd.DataFrame:
    today = date.today().isoformat()
    q = (f"applicationMetaData.applicationTypeCode:DES AND "
         f"applicationMetaData.grantDate:[{after_date} TO {today}]")
    url = f"{API_BASE}/api/v1/patent/applications/search"
    rows, offset, total, bar = [], 0, None, None
    while True:
        payload = {"q": q, "pagination": {"offset": offset, "limit": PAGE_SIZE},
                   "fields": ["applicationMetaData.patentNumber", "applicationMetaData.grantDate"]}
        r = request_with_retry(session, "POST", url, logger,
                               context=f"api supplement offset={offset}", json=payload, timeout=90)
        if r is None:
            sys.exit("API 補完に失敗しました。ネットワークを確認して再実行してください。")
        data = r.json()
        if total is None:
            total = data.get("count", 0)
            print(f"🔎 API 補完: {after_date} 〜 {today} の意匠登録 {total:,} 件")
            bar = tqdm(total=total, unit="件", desc="API補完", dynamic_ncols=True)
        items = data.get("patentFileWrapperDataBag", [])
        if not items:
            break
        for it in items:
            m = it.get("applicationMetaData", {})
            pnum, gdate = m.get("patentNumber"), m.get("grantDate")
            if pnum and gdate:
                rows.append((pnum, gdate))
        bar.update(len(items))
        offset += PAGE_SIZE
        if offset >= total:
            break
        time.sleep(0.3)
    if bar:
        bar.close()
    return pd.DataFrame(rows, columns=["patent_id", "grant_date"])


# ── マスタ ID の読み込み ──────────────────────────────────────────────────────

def load_master_ids() -> tuple[set[int], list[str]]:
    csv_files = sorted(MASTER_DATA_DIR.glob("*.csv"))
    if not csv_files:
        sys.exit(f"マスタ CSV が見つかりません: {MASTER_DATA_DIR}")
    ids: set[int] = set()
    samples: list[str] = []
    for f in tqdm(csv_files, desc="マスタ読込", unit="file", dynamic_ncols=True):
        s = pd.read_csv(f, usecols=["id"], dtype=str)["id"].dropna()
        if len(samples) < 100:
            samples.extend(s.head(100 - len(samples)).tolist())
        for v in s:
            n = id_to_int(v)
            if n is not None:
                ids.add(n)
    return ids, samples


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="IMPACT に不足する意匠特許の列挙")
    ap.add_argument("--refresh-bulk", action="store_true",
                    help="g_patent.tsv.zip を削除して再ダウンロードする")
    ap.add_argument("--no-api-supplement", action="store_true",
                    help="一括テーブル最終日以降の API 補完を行わない")
    args = ap.parse_args()

    ensure_dirs()
    api_key = load_api_key()
    session = api_session(api_key)

    # ── ダウンロード ──
    if args.refresh_bulk and G_PATENT_ZIP.exists():
        G_PATENT_ZIP.unlink()
    if not download_file(session, G_PATENT_URL, G_PATENT_ZIP, logger, desc="g_patent.tsv.zip"):
        sys.exit("g_patent.tsv.zip のダウンロードに失敗しました。再実行してください。")

    # ── ① 抽出 ──
    enum_df = load_design_grants_from_bulk()
    enum_df["source"] = "g_patent"

    if not args.no_api_supplement:
        max_date = enum_df["grant_date"].max()
        sup = supplement_from_api(session, max_date)
        if len(sup):
            sup["source"] = "odp_api"
            enum_df = pd.concat([enum_df, sup], ignore_index=True)
            print(f"   API 補完分を追加: {len(sup):,} 件 (一括テーブル最終日: {max_date})")

    # ── ② IMPACT 形式へ変換 (D+7桁ゼロ埋め) ──
    enum_df["patent_id"] = enum_df["patent_id"].map(to_impact_id)
    n_bad = enum_df["patent_id"].isna().sum()
    if n_bad:
        logger.warning(f"ID 変換不能: {n_bad} 件 (除外)")
        enum_df = enum_df.dropna(subset=["patent_id"])
    enum_df = enum_df.drop_duplicates(subset=["patent_id"])

    #    形式検証: 変換後の先頭 100 件がマスタの形式と一致しなければ停止
    master_ids, master_samples = load_master_ids()
    head100 = enum_df["patent_id"].head(100).tolist()
    if not all(IMPACT_ID_RE.match(x) for x in head100):
        bad = [x for x in head100 if not IMPACT_ID_RE.match(x)][:5]
        sys.exit(f"変換後 ID がマスタ形式 (D+7桁) と不一致のため停止: {bad}")
    master_bad = [x for x in master_samples if not IMPACT_ID_RE.match(str(x).strip())]
    if master_bad:
        sys.exit(f"マスタ側の ID 形式が想定 (D+7桁) と異なるため停止: {master_bad[:5]}")
    print(f"✅ 形式検証 OK: 変換後の先頭 100 件はマスタ形式 (例 {head100[0]}) と一致")

    # ── ③ マスタとの差分 ──
    enum_df["pid_int"] = enum_df["patent_id"].map(id_to_int)
    enum_ints = set(enum_df["pid_int"])
    missing_df = enum_df[~enum_df["pid_int"].isin(master_ids)].copy()
    missing_ints = set(missing_df["pid_int"])

    # ── 検証 (単体テスト) ──
    assert not (missing_ints & master_ids), "差分リストとマスタの共通部分が空ではありません"
    assert missing_ints | (enum_ints & master_ids) == enum_ints, \
        "差分リスト + (列挙∩マスタ) が全列挙と一致しません"
    print("✅ 検証 OK: 差分∩マスタ=∅ / 差分∪(列挙∩マスタ)=全列挙")

    # ── 出力 ──
    out = missing_df[["patent_id", "grant_date", "source"]].sort_values("grant_date")
    out.to_parquet(MISSING_PARQUET, index=False)

    print(f"\n📊 集計:")
    print(f"   列挙 (意匠・{CUTOFF_DATE} 以降): {len(enum_ints):,} 件")
    print(f"   マスタ:                        {len(master_ids):,} 件")
    print(f"   不足 (missing):                {len(out):,} 件")
    year_counts = out["grant_date"].str[:4].value_counts().sort_index()
    print("   不足件数 (年別):")
    for y, c in year_counts.items():
        print(f"     {y}: {c:,}")
    print(f"\n✅ 保存完了: {MISSING_PARQUET}")
    logger.info(f"missing={len(out)} enum={len(enum_ints)} master={len(master_ids)}")


if __name__ == "__main__":
    main()
