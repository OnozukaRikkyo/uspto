import argparse
import os
import glob
import json
import time
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 設定情報の定義
# ==========================================
ROOT_DIR = "/mnt/eightthdd/uspto"
DATA_DIR = f"{ROOT_DIR}/data"
OUTPUT_JSON_PATH = f"{ROOT_DIR}/examiner_rejections_all.json"   # 結果を保存するJSONファイルのパス
MY_API_KEY = os.getenv("MY_API_KEY")                  # USPTO ODP APIキー

def normalize_patent_id(raw_id):
    """
    CSVのID（例: D0949851）から、API検索用のコア番号（D949851）に正規化する。
    """
    raw_id = str(raw_id).strip()
    if raw_id.startswith('D'):
        # Dの後のゼロを消す (D000123 -> D123)
        return 'D' + str(int(raw_id[1:]))
    elif raw_id.isdigit():
        return str(int(raw_id))
    return raw_id

def extract_examiner_rejections(prior_art_number, api_key=None):
    url = "https://developer.uspto.gov/ds-api/enriched_cited_reference_metadata/v2/records"

    num = prior_art_number.replace("D", "")

    # 🔥 完全一致をやめる
    criteria = f'citedDocumentIdentifier:(*D{num}*)'

    payload = {
        "criteria": criteria,
        "start": 0,
        "rows": 100
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    if api_key:
        headers["X-API-KEY"] = api_key

    response = requests.post(url, headers=headers, data=payload)
    response.raise_for_status()

    docs = response.json().get("response", {}).get("docs", [])

    print(f"\nDEBUG: {prior_art_number}")
    print(f"criteria: {criteria}")
    print(f"総ヒット件数 = {len(docs)}")

    # 🔥 まずはフィルタなしで確認（重要）
    if len(docs) == 0:
        return []

    results = []

    for doc in docs:
        raw_flag = doc.get("applicantCitedExaminerReferenceIndicator", "")
        flag = str(raw_flag).lower()

        is_examiner = (
            raw_flag is True or
            "examiner" in flag or
            flag == "e"
        )

        if not is_examiner:
            continue

        results.append({
            "app": doc.get("patentApplicationNumber"),
            "date": doc.get("officeActionDate"),
            "cited": doc.get("citedDocumentIdentifier")
        })

    print(f"Examiner引用 = {len(results)}")

    return results


def process_csv_batch(skip_existing: bool = True):
    """
    CSVを読み込み、ループでAPI処理を実行し、結果を保存する
    """
    csv_files = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    print(f"📂 CSVファイルの読み込み中: {DATA_DIR} ({len(csv_files)}ファイル)")
    df = pd.concat([pd.read_csv(f) for f in csv_files], ignore_index=True)

    if 'id' not in df.columns:
        print("エラー: CSVに 'id' カラムが存在しません。")
        return

    # 重複を排除して処理対象のリストを作成
    raw_ids = df['id'].dropna().unique()
    total_count = len(raw_ids)
    print(f"🎯 処理対象となるユニークな特許件数: {total_count}件")
    print(f"⚙️  モード: {'スキップ (処理済みをスキップ)' if skip_existing else '上書き (全件再処理)'}\n")

    # 過去の実行結果（レジューム用）を読み込む
    results_dict = {}
    if os.path.exists(OUTPUT_JSON_PATH):
        try:
            with open(OUTPUT_JSON_PATH, "r", encoding="utf-8") as f:
                results_dict = json.load(f)
            print(f"🔄 既存のデータ（{len(results_dict)}件）を読み込みました。")
        except json.JSONDecodeError:
            print("⚠️ 既存のJSONファイルが壊れています。新規作成します。")

    # メインループ
    for i, raw_id in enumerate(raw_ids, 1):
        target_patent = normalize_patent_id(raw_id)

        if skip_existing and target_patent in results_dict:
            continue

        print(f"[{i}/{total_count}] 検索中: {target_patent} (元ID: {raw_id})")
        
        # APIリクエストの実行
        rejections = extract_examiner_rejections(target_patent, api_key=MY_API_KEY)
        
        # サーバー負荷軽減のためのスリープ（1件あたり1秒待つ）
        time.sleep(1.0)
        
        if rejections is not None:
            if len(rejections) > 0:
                results_dict[target_patent] = {
                    "original_id": raw_id,
                    "rejections_found": len(rejections),
                    "records": rejections
                }
                print(f"  👉 発見: {len(rejections)} 件の拒絶引用履歴を抽出しました。")
            else:
                print("  👉 発見なし")
        else:
            print("  👉 データの取得に失敗しました。")
            # 失敗した場合は dict に記録せず、次回実行時に再トライさせる
            continue
            
        # 1件処理するごとにJSONファイルに上書き保存（不意の停止対策）
        with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(results_dict, f, ensure_ascii=False, indent=2)

    print("\n✅ 全ての処理が完了しました！")
    print(f"出力ファイル: {os.path.abspath(OUTPUT_JSON_PATH)}")

def main():
    parser = argparse.ArgumentParser(description="USPTO examiner rejection extractor")
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="処理済みレコードを上書き再処理する（デフォルト: スキップ）",
    )
    parser.set_defaults(skip_existing=True)
    args = parser.parse_args()
    process_csv_batch(skip_existing=args.skip_existing)

if __name__ == "__main__":
    main()

