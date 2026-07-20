#!/usr/bin/env python3
"""
fetch_applicants.py — USPTO 全意匠特許の出願者（意匠を作った会社）一括取得

意匠を作った会社 = 出願時の申請者 = applicationMetaData.applicantBag[0].applicantNameText
  ※ 担保銀行（SECURITY AGREEMENT）・代理人・権利保持者 ではない

入力:
  /mnt/eightthdd/uspto/edge_list/*.csv の source/target 列に含まれる全特許 ID

出力:
  patent_applicants.csv    patent_id, applicant, current_owner, owner_country, n_assignments
  patent_applicants_cache.json  取得済み結果のキャッシュ（途中から再開可能）

使い方:
  source /home/sonozuka/network_fig/venv/bin/activate
  python3 /home/sonozuka/uspto/fetch_applicants.py            # 全件取得
  python3 /home/sonozuka/uspto/fetch_applicants.py --dry-run  # 件数確認のみ
  python3 /home/sonozuka/uspto/fetch_applicants.py --sleep 1.0 --save-every 200
  python3 /home/sonozuka/uspto/fetch_applicants.py --input /path/to/edge_list
"""

import argparse
import glob
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from tqdm import tqdm

# ── デフォルトパス ──────────────────────────────────────────────────────────────
DEFAULT_EDGE_DIR  = Path("/mnt/eightthdd/uspto/edge_list")
DEFAULT_CACHE     = Path("/mnt/eightthdd/uspto/patent_applicants_cache.json")
DEFAULT_OUTPUT    = Path("/mnt/eightthdd/uspto/patent_applicants.csv")
DEFAULT_LOG_DIR   = Path("/mnt/eightthdd/uspto/log")
ENV_FILE          = Path(__file__).parent / ".env"

# ── API 設定 ───────────────────────────────────────────────────────────────────
BASE_URL   = "https://api.uspto.gov"
BATCH_SIZE = 50       # 100件は API が 0件返す（URLが長すぎる）
TIMEOUT    = 90       # バッチクエリは遅いため長めに

# SECURITY AGREEMENT（担保）は除外する
_SECURITY_KW = ("SECURITY AGREEMENT", "SECURITY INTEREST", "COLLATERAL AGENT")


# ──────────────────────────────────────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────────────────────────────────────

def setup_logger(log_dir: Path, name: str = "fetch_applicants") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def normalize_id(raw: str) -> str:
    """'D0534337' → 'D534337'（API クエリ用・先頭ゼロ除去）"""
    s = str(raw).strip()
    if s.upper().startswith("D") and s[1:].isdigit():
        return "D" + str(int(s[1:]))
    return s


def is_security(conveyance: str) -> bool:
    upper = str(conveyance).upper()
    return any(kw in upper for kw in _SECURITY_KW)


def extract_info(item: dict) -> dict:
    """patentFileWrapperDataBag の1エントリから出願者・所有者を抽出"""
    meta     = item.get("applicationMetaData", {})
    app_bag  = meta.get("applicantBag", [])
    applicant = app_bag[0].get("applicantNameText", "") if app_bag else ""

    assignments = item.get("assignmentBag", [])
    legit = [a for a in assignments if not is_security(a.get("conveyanceText", ""))]

    current_owner = owner_country = ""
    if legit:
        latest   = max(legit, key=lambda a: a.get("assignmentRecordedDate", ""))
        anames   = latest.get("assigneeBag", [])
        current_owner = anames[0].get("assigneeNameText", "") if anames else ""
        addr     = anames[0].get("assigneeAddress", {}) if anames else {}
        owner_country = addr.get("ictCountryCode", "")

    return {
        "applicant":     applicant,
        "current_owner": current_owner,
        "owner_country": owner_country,
        "n_assignments": len(assignments),
    }


# ──────────────────────────────────────────────────────────────────────────────
# ID 収集
# ──────────────────────────────────────────────────────────────────────────────

def collect_ids(edge_dir: Path) -> list[str]:
    """edge_list/*.csv の source/target から全ユニーク特許 ID を返す（元フォーマット保持）"""
    files = sorted(edge_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"edge_list に CSV がありません: {edge_dir}")

    raw_ids: set[str] = set()
    for f in files:
        df = pd.read_csv(f, usecols=["source", "target"], dtype=str)
        raw_ids.update(df["source"].dropna())
        raw_ids.update(df["target"].dropna())

    # 正規化した ID をキー、元 ID をバリューとして重複排除
    norm_to_raw: dict[str, str] = {}
    for rid in raw_ids:
        norm = normalize_id(rid)
        if norm not in norm_to_raw:
            norm_to_raw[norm] = rid   # 最初に出た元 ID を代表値として保持

    # 正規化済みリスト（ソート）
    return sorted(norm_to_raw.keys())


# ──────────────────────────────────────────────────────────────────────────────
# バッチ取得
# ──────────────────────────────────────────────────────────────────────────────

def fetch_batch(
    patent_numbers: list[str],
    session: requests.Session,
    logger: logging.Logger,
    retry_sleep: float,
) -> dict[str, dict]:
    """50件以下の特許番号を OR クエリで一括取得。取得できなかった ID は空値で返す。"""
    empty  = {"applicant": "", "current_owner": "", "owner_country": "", "n_assignments": 0}
    result = {p: dict(empty) for p in patent_numbers}

    q       = "applicationMetaData.patentNumber:(" + " OR ".join(patent_numbers) + ")"
    payload = {"q": q, "pagination": {"offset": 0, "limit": BATCH_SIZE}}
    context = f"batch({len(patent_numbers)}): {patent_numbers[0]}…"

    for attempt in range(2):
        try:
            r = session.post(
                f"{BASE_URL}/api/v1/patent/applications/search",
                json=payload,
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"通信エラー: {e} | {context}")
            if attempt == 0:
                time.sleep(retry_sleep)
                continue
            return result

        if r.status_code != 200:
            logger.warning(f"HTTP {r.status_code} | {context}")
            if attempt == 0:
                time.sleep(retry_sleep)
                continue
            return result
        break   # 成功

    try:
        data = r.json()
    except Exception as e:
        logger.error(f"JSON decode error: {e} | {context}")
        return result

    for item in data.get("patentFileWrapperDataBag", []):
        meta = item.get("applicationMetaData", {})
        pnum = meta.get("patentNumber", "")
        if pnum in result:
            result[pnum] = extract_info(item)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# キャッシュ I/O
# ──────────────────────────────────────────────────────────────────────────────

def load_cache(cache_path: Path) -> dict:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict, cache_path: Path) -> None:
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def save_csv(cache: dict, all_norm_ids: list[str], output_path: Path) -> None:
    """キャッシュから CSV を生成（取得できなかった ID は空値）"""
    empty = {"applicant": "", "current_owner": "", "owner_country": "", "n_assignments": 0}
    rows  = []
    for norm in all_norm_ids:
        info = cache.get(norm, empty)
        rows.append({
            "patent_id_normalized": norm,
            "applicant":            info.get("applicant", ""),
            "current_owner":        info.get("current_owner", ""),
            "owner_country":        info.get("owner_country", ""),
            "n_assignments":        info.get("n_assignments", 0),
        })
    pd.DataFrame(rows).to_csv(output_path, index=False)


# ──────────────────────────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="USPTO 全意匠特許の出願者（意匠を作った会社）を一括取得する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input",      type=Path, default=DEFAULT_EDGE_DIR,
                        help="edge_list CSVs が入ったディレクトリ")
    parser.add_argument("--output",     type=Path, default=DEFAULT_OUTPUT,
                        help="出力 CSV のパス")
    parser.add_argument("--cache",      type=Path, default=DEFAULT_CACHE,
                        help="キャッシュ JSON のパス（途中再開用）")
    parser.add_argument("--log-dir",    type=Path, default=DEFAULT_LOG_DIR,
                        help="ログ出力先ディレクトリ")
    parser.add_argument("--sleep",      type=float, default=0.5,
                        help="バッチ間の待機秒数")
    parser.add_argument("--retry-sleep",type=float, default=15.0,
                        help="エラー時のリトライ待機秒数")
    parser.add_argument("--save-every", type=int,   default=300,
                        help="N バッチごとに中間 CSV を保存")
    parser.add_argument("--dry-run",    action="store_true",
                        help="実際に API を叩かず件数・推計時間だけ表示する")
    args = parser.parse_args()

    # ── 環境設定 ──────────────────────────────────────────────────────────────
    load_dotenv(ENV_FILE)
    api_key = os.getenv("MY_API_KEY") or os.getenv("PATENT_CLIENT_ODP_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit(f"API キーが見つかりません。{ENV_FILE} に MY_API_KEY を設定してください。")

    logger = setup_logger(args.log_dir)

    # ── ID 収集 ───────────────────────────────────────────────────────────────
    print("📂 特許 ID を収集中...")
    all_ids = collect_ids(args.input)
    print(f"   全ユニーク特許 ID: {len(all_ids):,}")

    # ── キャッシュ読み込み ────────────────────────────────────────────────────
    cache = load_cache(args.cache)
    # 新フォーマット（applicant キーあり）のみ有効とみなす
    cache = {k: v for k, v in cache.items() if "applicant" in v}
    cached_count = sum(1 for nid in all_ids if nid in cache)
    todo          = [nid for nid in all_ids if nid not in cache]

    print(f"   キャッシュ済み:    {cached_count:,}")
    print(f"   取得対象:          {len(todo):,}")

    n_batches = (len(todo) + BATCH_SIZE - 1) // BATCH_SIZE
    est_sec   = n_batches * (4.5 + args.sleep)   # 実績 ~4-5s/batch
    print(f"   バッチ数:          {n_batches:,}")
    print(f"   推計時間:          {timedelta(seconds=int(est_sec))}")
    print(f"   出力先:            {args.output}")
    print(f"   キャッシュ:        {args.cache}")

    if args.dry_run:
        print("\n[DRY-RUN] API リクエストは行いません。")
        return

    if not todo:
        print("\n全件キャッシュ済み。CSV を出力して終了します。")
        save_csv(cache, all_ids, args.output)
        print(f"✅ 保存完了: {args.output}")
        return

    # ── バッチ処理 ────────────────────────────────────────────────────────────
    headers = {
        "X-API-KEY":    api_key,
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }
    batches   = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    start_t   = time.time()
    hit_total = 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.cache.parent.mkdir(parents=True, exist_ok=True)

    with requests.Session() as session:
        session.headers.update(headers)

        for batch_idx, batch in enumerate(
            tqdm(batches, desc="Fetching applicants", unit="batch"), start=1
        ):
            results  = fetch_batch(batch, session, logger, args.retry_sleep)
            hits     = sum(1 for v in results.values() if v.get("applicant"))
            hit_total += hits
            cache.update(results)

            tqdm.write(
                f"  [{batch_idx:>5}/{n_batches}]"
                f"  cached={len(cache):>7,}"
                f"  hit={hits}/{len(batch)}"
                f"  ex: {next((v['applicant'] for v in results.values() if v.get('applicant')), '—')[:40]}"
            )

            # キャッシュを逐次保存（途中停止 → 再実行で継続可能）
            save_cache(cache, args.cache)

            # 中間 CSV
            if batch_idx % args.save_every == 0:
                save_csv(cache, all_ids, args.output)
                elapsed = time.time() - start_t
                remain  = elapsed / batch_idx * (n_batches - batch_idx)
                tqdm.write(
                    f"  [中間保存] {args.output.name}  "
                    f"経過: {timedelta(seconds=int(elapsed))}  "
                    f"残り推計: {timedelta(seconds=int(remain))}"
                )

            time.sleep(args.sleep)

    # ── 最終 CSV ──────────────────────────────────────────────────────────────
    save_csv(cache, all_ids, args.output)

    elapsed = time.time() - start_t
    n_have  = sum(1 for nid in all_ids if cache.get(nid, {}).get("applicant"))
    print(f"\n✅ 完了: {args.output}")
    print(f"   総 ID:       {len(all_ids):,}")
    print(f"   applicant あり: {n_have:,} ({100*n_have/max(len(all_ids),1):.1f}%)")
    print(f"   処理時間:    {timedelta(seconds=int(elapsed))}")

    # 上位 applicant
    df = pd.read_csv(args.output)
    top = df[df["applicant"] != ""]["applicant"].value_counts().head(20)
    print("\n上位 applicant（件数順）:")
    print(top.to_string())


if __name__ == "__main__":
    main()
