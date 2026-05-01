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

# ==========================================
# 設定情報の定義
# ==========================================
ROOT_DIR = "/mnt/eightthdd/uspto"
DATA_DIR = f"{ROOT_DIR}/data"
JSON_OUTPUT_DIR = Path(f"{ROOT_DIR}/json")
CANDIDATES_LOG_PATH = f"{ROOT_DIR}/processed_log_all.txt"          # 検索完了ログ（再開用）
STRICT_JSON_PATH = f"{ROOT_DIR}/layer2_strict_102_103.json"        # 💎 Layer 2: テキストで明確な拒絶が裏付けられた確証ペア
PTO892_JSON_PATH = f"{ROOT_DIR}/layer1_pto892_candidates.json"     # 🥇 Layer 1: 審査官が引用した強力な類似候補（AI学習の主データ）
MY_API_KEY = os.getenv("MY_API_KEY")
logger = setup_logger("uspto_2")

def normalize_patent_id(raw_id):
    raw_id = str(raw_id).strip()
    if raw_id.startswith('D'):
        return 'D' + str(int(raw_id[1:]))
    elif raw_id.isdigit():
        return str(int(raw_id))
    return raw_id

def extract_layer1_candidates(prior_art_number, api_key, known_ids=None):
    """
    【第1段階】Citation APIから「審査官が引用した」候補リストを幅広く抽出する
    """
    url = "https://developer.uspto.gov/ds-api/enriched_cited_reference_metadata/v2/records"
    num = prior_art_number.replace("D", "")
    criteria = f'citedDocumentIdentifier:(*D{num}*)'

    payload = {"criteria": criteria, "start": 0, "rows": 50}
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if api_key:
        headers["X-API-KEY"] = api_key

    context = f"Layer1 patent={prior_art_number} url={url}"
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        check_status(response, context, logger)
    except requests.exceptions.RequestException as e:
        logger.error(f"通信エラー: {e} | {context}")
        return None

    docs = response.json().get("response", {}).get("docs", [])
    all_docs = []
    candidates = []

    for doc in docs:
        if not doc.get("officeActionDate"):
            continue
        check_and_register_cited(doc, known_ids)

        all_docs.append(doc)

        is_examiner = doc.get("examinerCitedReferenceIndicator", False)
        alt_ind = doc.get("applicantCitedExaminerReferenceIndicator", False)

        # 審査官による引用(PTO-892)であること
        if is_examiner is True or alt_ind is True or str(alt_ind).lower() == "e":
            oa_category = str(doc.get("officeActionCategory", "")).upper()

            # CTNF/CTFRだけでなく、NONやFINALが含まれるOAを広く拾う
            if any(x in oa_category for x in ["CTNF", "CTFR", "NON", "FINAL"]):
                candidates.append({
                    "app_number": doc.get("patentApplicationNumber"),
                    "oa_date": doc.get("officeActionDate"),
                    "oa_category": oa_category
                })
    return candidates, all_docs

def verify_layer2_strict(app_number, target_patent, api_key):
    """
    【第2段階】Office Action APIで実際のテキストを取得し、Layer 2へ格上げできるか検証
    戻り値: (状態コード, 理由)
    """
    url = "https://developer.uspto.gov/ds-api/oa_actions/v1/records"
    criteria = f'patentApplicationNumber:{app_number}'

    payload = {"criteria": criteria, "start": 0, "rows": 5}
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if api_key:
        headers["X-API-KEY"] = api_key

    context = f"Layer2 app={app_number} target={target_patent} url={url}"
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        check_status(response, context, logger)
    except requests.exceptions.RequestException as e:
        logger.error(f"通信エラー: {e} | {context}")
        return "KEEP_LAYER1", f"通信エラー ({e})"

    docs = response.json().get("response", {}).get("docs", [])

    # テキストがない場合も失敗ではなく「Layer 1として保持」とする
    if not docs:
        return "KEEP_LAYER1", "テキスト未収録(PDFのみの可能性)"

    full_text = " ".join([doc.get("officeActionText", "") for doc in docs]).lower()

    # ==========================================
    # 1. 拒絶の「法的文脈」があるかどうかの厳密チェック
    # ==========================================
    rejection_phrases = [
        "rejected under", "rejection under",
        "refused under", "refusal under",
        "unpatentable over", "obvious over", "anticipated by",
        "notification of refusal"
    ]

    if not any(phrase in full_text for phrase in rejection_phrases):
        return "KEEP_LAYER1", "テキスト有だが明確な拒絶の法的文脈なし"

    # ==========================================
    # 2. その拒絶文脈の中で「ターゲット特許」が直接引用されているかのチェック
    # ==========================================
    num = target_patent.replace("D", "")
    num_comma = f"{num[:3]},{num[3:]}" if len(num) == 6 else num
    patterns = [f"d{num}", f"d{num_comma}", num, num_comma]

    for p in patterns:
        if p in full_text:
            return "STRICT", "法的文脈(102/103等)と対象特許の直接引用を確認！"

    return "KEEP_LAYER1", "法的文脈はあるが対象特許の直接引用なし"

def process_hybrid_pipeline(skip_existing: bool = True):
    csv_files = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    JSON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📂 入力ディレクトリ: {DATA_DIR} ({len(csv_files)}ファイル)")
    print(f"📁 JSON出力先: {JSON_OUTPUT_DIR}")
    print(f"⚙️  モード: {'スキップ (処理済みをスキップ)' if skip_existing else '上書き (全件再処理)'}\n")

    print("🔍 既知特許IDセットを構築中...")
    known_ids = load_known_ids(DATA_DIR)
    print(f"   → {len(known_ids):,} 件のIDを読み込みました。\n")

    # 💎 Layer 2 (確証ペア) の読み込み
    layer2_results = {}
    if os.path.exists(STRICT_JSON_PATH):
        with open(STRICT_JSON_PATH, "r", encoding="utf-8") as f:
            layer2_results = json.load(f)

    # 🥇 Layer 1 (PTO-892 主力候補) の読み込み
    layer1_results = {}
    if os.path.exists(PTO892_JSON_PATH):
        try:
            with open(PTO892_JSON_PATH, "r", encoding="utf-8") as f:
                layer1_results = json.load(f)
        except json.JSONDecodeError:
            pass

    # 処理済みログの読み込み
    processed_ids = set()
    if os.path.exists(CANDIDATES_LOG_PATH):
        with open(CANDIDATES_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                processed_ids.add(line.strip())

    print(f"🔄 済: {len(processed_ids)}件 | Layer1: {len(layer1_results)}件 | Layer2: {len(layer2_results)}件\n")

    for csv_path in csv_files:
        df = pd.read_csv(csv_path)
        if 'id' not in df.columns:
            tqdm.write(f"[SKIP] {csv_path}: 'id' カラムが存在しません。")
            continue

        raw_ids = df['id'].dropna().unique()
        csv_stem = Path(csv_path).stem
        csv_name = Path(csv_path).name
        json_path = JSON_OUTPUT_DIR / f"{csv_stem}.json"

        csv_results = {}
        if json_path.exists():
            try:
                with open(json_path, encoding="utf-8") as f:
                    csv_results = json.load(f)
            except json.JSONDecodeError:
                pass

        for raw_id in tqdm(raw_ids, desc=csv_name, unit="件"):
            target_patent = normalize_patent_id(raw_id)

            if skip_existing and target_patent in processed_ids:
                continue

            result = extract_layer1_candidates(target_patent, api_key=MY_API_KEY, known_ids=known_ids)
            time.sleep(1.0)

            if result is None:
                tqdm.write(f"  [ERROR] {target_patent}: 通信エラー。次回再試行します。")
                continue

            candidates, all_docs = result

            # per-CSV JSON に全フィールドを保存
            if all_docs:
                csv_results[target_patent] = {
                    "original_id": str(raw_id),
                    "citations_found": len(all_docs),
                    "records": all_docs
                }
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(csv_results, f, ensure_ascii=False, indent=2)
                tqdm.write(f"  📄 {target_patent}: {len(all_docs)}件のcitationを {json_path.name} に保存")

            layer2_strict = []
            layer1_pto892 = []

            if len(candidates) > 0:
                tqdm.write(f"  👉 {target_patent}: 候補 {len(candidates)}件 → 第2段階へ")

                for cand in candidates:
                    app_num = cand["app_number"]
                    status, reason = verify_layer2_strict(app_num, target_patent, api_key=MY_API_KEY)
                    time.sleep(1.0)

                    cand["verification_status"] = reason
                    if status == "STRICT":
                        tqdm.write(f"    💎 [Layer 2] 出願 {app_num}: {reason}")
                        layer2_strict.append(cand)
                    else:
                        tqdm.write(f"    🥇 [Layer 1] 出願 {app_num}: {reason}")
                        layer1_pto892.append(cand)

            if len(layer2_strict) > 0:
                layer2_results[target_patent] = {
                    "original_id": raw_id,
                    "strict_pairs_found": len(layer2_strict),
                    "records": layer2_strict
                }
                with open(STRICT_JSON_PATH, "w", encoding="utf-8") as f:
                    json.dump(layer2_results, f, ensure_ascii=False, indent=2)
                tqdm.write(f"  🎉 {target_patent}: Layer 2ペア {len(layer2_strict)}件を保存")

            if len(layer1_pto892) > 0:
                layer1_results[target_patent] = {
                    "original_id": raw_id,
                    "pto892_candidates_found": len(layer1_pto892),
                    "records": layer1_pto892
                }
                with open(PTO892_JSON_PATH, "w", encoding="utf-8") as f:
                    json.dump(layer1_results, f, ensure_ascii=False, indent=2)
                tqdm.write(f"  📝 {target_patent}: Layer 1候補 {len(layer1_pto892)}件を保存")

            processed_ids.add(target_patent)
            with open(CANDIDATES_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(target_patent + "\n")

    print("\n✅ 全パイプラインが完了しました！")

def main():
    parser = argparse.ArgumentParser(description="USPTO hybrid pipeline (Layer1 + Layer2)")
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="処理済みレコードを上書き再処理する（デフォルト: スキップ）",
    )
    parser.set_defaults(skip_existing=True)
    args = parser.parse_args()
    process_hybrid_pipeline(skip_existing=args.skip_existing)

if __name__ == "__main__":
    main()