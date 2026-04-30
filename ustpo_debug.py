import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

def test_single_patent(target_patent, api_key):
    url = "https://developer.uspto.gov/ds-api/enriched_cited_reference_metadata/v2/records"
    
    # 【修正】カッコ()を外して、純粋なワイルドカードに変更
    criteria = f'citedDocumentIdentifier:*{target_patent}*'
    
    payload = {
        "criteria": criteria,
        "start": 0,
        "rows": 10
    }
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-API-KEY": api_key
    }

    print(f"🔍 検索クエリ: {criteria} でリクエストを送信中...\n")
    
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # APIが持っている件数の総数
        num_found = data.get("response", {}).get("numFound", 0)
        docs = data.get("response", {}).get("docs", [])
        
        print(f"✅ APIヒット総数: {num_found} 件\n")
        
        if docs:
            print("【取得した生データの先頭1件を表示します】")
            print("-" * 50)
            first_doc = docs[0]
            app_num = first_doc.get("patentApplicationNumber", "不明")
            cited_by = first_doc.get("applicantCitedExaminerReferenceIndicator", "不明")
            
            print(f"出願番号: {app_num}")
            print(f"引用者フラグ: {cited_by}")
            print("--- JSON全体 ---")
            print(json.dumps(first_doc, indent=2, ensure_ascii=False))
            print("-" * 50)
        else:
            print("❌ データが1件も取得できませんでした。")
            
    except Exception as e:
        print(f"エラーが発生しました: {e}")

if __name__ == "__main__":
    MY_API_KEY = os.getenv("MY_API_KEY")
    TARGET_PATENT = "D714742"

    test_single_patent(TARGET_PATENT, MY_API_KEY)