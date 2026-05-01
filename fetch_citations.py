import argparse
import os
import glob
import json
import time
import requests
import pandas as pd
from dotenv import load_dotenv
from pathlib import Path
from tqdm import tqdm
from api_utils import setup_logger, check_status, load_known_ids, check_and_register_cited

load_dotenv()

ROOT_DIR = "/mnt/eightthdd/uspto"
DATA_DIR = f"{ROOT_DIR}/data"
JSON_OUTPUT_DIR = Path(f"{ROOT_DIR}/json")
CANDIDATES_LOG_PATH = f"{ROOT_DIR}/processed_log_all.txt"
MY_API_KEY = os.getenv("MY_API_KEY")
logger = setup_logger("fetch_citations")

_EXCLUDE_FIELDS = {"createUserIdentifier", "obsoleteDocumentIdentifier",
                   "qualitySummaryText", "createDateTime", "id"}

def normalize_patent_id(raw_id):
    raw_id = str(raw_id).strip()
    if raw_id.startswith('D'):
        return 'D' + str(int(raw_id[1:]))
    elif raw_id.isdigit():
        return str(int(raw_id))
    return raw_id

def fetch_citations(prior_art_number, api_key, known_ids=None):
    url = "https://developer.uspto.gov/ds-api/enriched_cited_reference_metadata/v2/records"
    num = prior_art_number.replace("D", "")
    criteria = f'citedDocumentIdentifier:(*D{num}*)'

    payload = {"criteria": criteria, "start": 0, "rows": 50}
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if api_key:
        headers["X-API-KEY"] = api_key

    context = f"patent={prior_art_number} url={url}"
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        check_status(response, context, logger)
    except requests.exceptions.RequestException as e:
        logger.error(f"通信エラー: {e} | {context}")
        return None

    docs = response.json().get("response", {}).get("docs", [])
    result = []
    for doc in docs:
        if not doc.get("officeActionDate"):
            continue
        check_and_register_cited(doc, known_ids)
        result.append({k: v for k, v in doc.items() if k not in _EXCLUDE_FIELDS})
    return result

def process(skip_existing: bool = True):
    csv_files = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    JSON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📂 入力: {DATA_DIR} ({len(csv_files)}ファイル)")
    print(f"📁 出力: {JSON_OUTPUT_DIR}")
    print(f"⚙️  モード: {'スキップ (処理済みをスキップ)' if skip_existing else '上書き (全件再処理)'}\n")

    print("🔍 既知特許IDセットを構築中...")
    known_ids = load_known_ids(DATA_DIR)
    print(f"   → {len(known_ids):,} 件のIDを読み込みました。\n")

    processed_ids = set()
    if os.path.exists(CANDIDATES_LOG_PATH):
        with open(CANDIDATES_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                processed_ids.add(line.strip())

    print(f"🔄 処理済み: {len(processed_ids)}件\n")

    for csv_path in csv_files:
        df = pd.read_csv(csv_path)
        if 'id' not in df.columns:
            tqdm.write(f"[SKIP] {csv_path}: 'id' カラムなし")
            continue

        csv_stem = Path(csv_path).stem
        json_path = JSON_OUTPUT_DIR / f"{csv_stem}.json"

        csv_results = {}
        if json_path.exists():
            try:
                with open(json_path, encoding="utf-8") as f:
                    csv_results = json.load(f)
            except json.JSONDecodeError:
                pass

        for raw_id in tqdm(df['id'].dropna().unique(), desc=Path(csv_path).name, unit="件"):
            target_patent = normalize_patent_id(raw_id)

            if skip_existing and target_patent in processed_ids:
                continue

            docs = fetch_citations(target_patent, api_key=MY_API_KEY, known_ids=known_ids)
            time.sleep(1.0)

            if docs is None:
                tqdm.write(f"  [ERROR] {target_patent}: 通信エラー。次回再試行します。")
                continue

            if docs:
                csv_results[target_patent] = {
                    "original_id": str(raw_id),
                    "citations_found": len(docs),
                    "records": docs
                }
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(csv_results, f, ensure_ascii=False, indent=2)
                tqdm.write(f"  📄 {target_patent}: {len(docs)}件 → {json_path.name}")

            processed_ids.add(target_patent)
            with open(CANDIDATES_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(target_patent + "\n")

    print("\n✅ 完了")

def main():
    parser = argparse.ArgumentParser(description="USPTO citation fetcher")
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="処理済みレコードを上書き再処理する（デフォルト: スキップ）",
    )
    parser.set_defaults(skip_existing=True)
    args = parser.parse_args()
    process(skip_existing=args.skip_existing)

if __name__ == "__main__":
    main()