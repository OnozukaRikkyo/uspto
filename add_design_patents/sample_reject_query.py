#!/usr/bin/env python3
"""
sample_reject_query.py — デザイン類似性による拒絶(reject)理由を USPTO API から取得するサンプル

doc/調査記録_reject_デザイン類似性.md の調査を再現する自己完結型スクリプト。
以下3段階のAPI呼び出しで、「意匠が類似・同一であることを理由とする拒絶」の
実際の文章まで辿り着く手順を示す。

  ① Patent Search API          : 特許番号 → 出願番号 の解決
  ② Enriched Citation API v3   : その特許が他の出願で審査官引用された記録一覧を取得し、
                                  citationCategoryCode (X/Y/A) で「拒絶根拠になったか」を判定
  ③ Office Action Text API     : ②でヒットした「引用した側の出願番号」の office action 全文を取得し、
                                  sections.detailCitationText / section103RejectionText など
                                  実際の拒絶理由の文章を取り出す

citationCategoryCode の意味 (国際的な先行技術調査でも使われる標準区分):
  X = その引用単独で新規性(35 U.S.C. 102)を否定する           → 事実上「同一」
  Y = 他の引用と組み合わせて非自明性(35 U.S.C. 103)を否定する → 事実上「類似」
  A = 背景として関連するのみ (拒絶の根拠ではない)

⚠️ 重要な注意 (doc/調査記録_reject_デザイン類似性.md の再検証で判明、全8件を本文照合した実績あり):
  citationCategoryCode が X/Y と判定されたレコードでも、実際に oa_actions から取得できる
  拒絶理由本文に、その特許番号自体への言及が**含まれないことが多い**(意匠のRosen型拒絶では
  基本引例が意匠特許である必要はなく実用文献でもよいため、同一 office action 内の
  別セクション/別引例を論じている場合がある。また過去に取得された引用が現在のライブAPIでは
  再現できないケースや、oa_actions 側が本文自体を保持していないケースもある)。
  このスクリプトは取得した本文中に対象特許番号への直接言及があるかを自動チェックし、
  無い場合は明示的にその旨を表示する。実測では検証した8件中、本文で確認できたのは2件のみ
  (25%)で、かつその1件は第三者との類似性ではなく**同一出願人の関連出願同士の二重特許
  (nonstatutory double patenting)**の理由だった。「他者の意匠に類似するとして拒絶された」
  ケースを探す場合は、本文を実際に読んで二重特許でないことも確認すること。

使い方:
  source /home/sonozuka/network_fig/venv/bin/activate
  python3 sample_reject_query.py D908314              # この特許を引用した拒絶を調べる
  python3 sample_reject_query.py D908314 --oa-text     # 該当する office action 全文も表示する
"""
import argparse
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

API_BASE = "https://api.uspto.gov"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 実際の拒絶理由文中に現れやすい、デザイン類似性を論じる表現 (参考用のハイライト)
_SIMILARITY_PHRASES = (
    "basically the same as", "substantially the same as", "identical to",
    "differs from", "similar to", "in re rosen", "in re durling",
    "obvious over", "unpatentable over", "anticipated by",
)


def to_api_id(raw: str) -> str:
    """'D0908314' → 'D908314' (先頭ゼロ除去。API クエリ用)"""
    s = raw.strip().upper()
    if s.startswith("D") and s[1:].isdigit():
        return "D" + str(int(s[1:]))
    return s


def load_session() -> requests.Session:
    load_dotenv(ENV_FILE)
    api_key = os.getenv("MY_API_KEY")
    if not api_key:
        sys.exit(f"API キーが見つかりません。{ENV_FILE} に MY_API_KEY を設定してください。")
    s = requests.Session()
    s.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})
    return s


# ── ① 特許番号 → 出願番号 ─────────────────────────────────────────────────────

def resolve_application_number(session: requests.Session, patent_id: str) -> str | None:
    url = f"{API_BASE}/api/v1/patent/applications/search"
    payload = {"q": f"applicationMetaData.patentNumber:({to_api_id(patent_id)})"}
    r = session.post(url, json=payload, timeout=30)
    r.raise_for_status()
    items = r.json().get("patentFileWrapperDataBag", [])
    if not items:
        return None
    return items[0].get("applicationNumberText")


# ── ② Enriched Citation API v3: 審査官引用の一覧を取得 ───────────────────────

def fetch_citing_records(session: requests.Session, patent_id: str) -> list[dict]:
    """
    patent_id が他の出願の office action 内で審査官引用(examinerCitedReferenceIndicator=true)
    された記録を返す。citationCategoryCode が X/Y のものが「拒絶根拠になった」候補。
    """
    url = f"{API_BASE}/api/v1/patent/oa/enriched_cited_reference_metadata/v3/records"
    api_id = to_api_id(patent_id)
    payload = f"criteria=citedDocumentIdentifier:(*{api_id}*)&start=0&rows=100"
    r = session.post(url, data=payload, timeout=60,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    r.raise_for_status()
    docs = r.json().get("response", {}).get("docs", [])
    return [d for d in docs if d.get("examinerCitedReferenceIndicator") is True]


# ── ③ Office Action Text API: 拒絶理由の実文章を取得 ──────────────────────────

def fetch_office_action_text(session: requests.Session, citing_app_number: str) -> list[dict]:
    """
    citing_app_number (引用した側の出願番号) の office action 本文を取得する。
    sections.detailCitationText に拒絶理由の全文が入っていることが多い
    (sections.section102RejectionText / section103RejectionText は空のことも多い)。
    """
    url = f"{API_BASE}/api/v1/patent/oa/oa_actions/v1/records"
    payload = f"criteria=patentApplicationNumber:{citing_app_number}&start=0&rows=10"
    r = session.post(url, data=payload, timeout=60,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", [])


def highlight_similarity_phrases(text: str) -> list[str]:
    lower = text.lower()
    return [p for p in _SIMILARITY_PHRASES if p in lower]


def mentions_target_patent(text: str, patent_id: str) -> bool:
    """
    本文中に対象特許番号への直接言及があるかを確認する。
    'D908314' → 'd908314' / 'd908,314' / 'd 908,314' / 'd 908314' の表記ゆれを試す
    (実際の拒絶本文は 'US D908,314 S' のようにカンマ入りで書かれることが多い)。
    裸の数字だけでの一致は他の数値(段落番号・出願番号の一部等)と衝突しやすく誤検知の原因になる
    ため、必ず 'd' プレフィックスと単語境界を要求する。
    """
    num = patent_id.strip().upper().lstrip("D")
    if not num.isdigit():
        return False
    num = str(int(num))
    num_comma = f"{num[:-3]},{num[-3:]}" if len(num) > 3 else num
    pattern = re.compile(
        r'\bd\.?\s*0*(?:' + re.escape(num) + '|' + re.escape(num_comma) + r')\b',
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="デザイン類似性による拒絶理由をUSPTO APIから取得する")
    ap.add_argument("patent_id", help="意匠特許番号 (例: D908314 / D0908314)")
    ap.add_argument("--oa-text", action="store_true",
                    help="citationCategoryCode が X/Y の場合、office action 全文も取得して表示する")
    args = ap.parse_args()

    session = load_session()
    patent_id = args.patent_id

    print(f"=== ① 出願番号を解決: {patent_id} ===")
    app_num = resolve_application_number(session, patent_id)
    print(f"  自身の出願番号: {app_num or '(見つからず)'}")

    print(f"\n=== ② {patent_id} が引用された記録 (審査官引用のみ) ===")
    records = fetch_citing_records(session, patent_id)
    print(f"  審査官引用の総数: {len(records)}")

    xy_records = [r for r in records if r.get("citationCategoryCode") in ("X", "Y")]
    print(f"  うち citationCategoryCode が X/Y (拒絶根拠の可能性): {len(xy_records)}")

    for rec in records:
        code = rec.get("citationCategoryCode")
        marker = "🔴" if code in ("X", "Y") else "  "
        print(f"  {marker} 引用先出願={rec.get('patentApplicationNumber')}  "
             f"OA種別={rec.get('officeActionCategory')}  code={code}  "
             f"日付={rec.get('officeActionDate','')[:10]}  "
             f"該当クレーム={rec.get('relatedClaimNumberText','')}")
        if rec.get("passageLocationText"):
            print(f"       該当箇所: {rec['passageLocationText'][0][:120]}")

    if not args.oa_text:
        print("\n(--oa-text を付けると、実際の拒絶理由の文章まで取得します)")
        return

    if not xy_records:
        print("\nX/Y 判定の記録がないため、office action 全文の取得はスキップします。")
        return

    print("\n=== ③ 拒絶理由の実文章 (office action 全文) ===")
    seen_apps = set()
    for rec in xy_records:
        citing_app = rec.get("patentApplicationNumber")
        if not citing_app or citing_app in seen_apps:
            continue
        seen_apps.add(citing_app)

        print(f"\n--- 出願 {citing_app} の office action ---")
        found_any_text = False
        found_target_mention = False
        for doc in fetch_office_action_text(session, citing_app):
            # 101/102/103/112 いずれかの拒絶セクション + detailCitationText を全てチェックする
            # (対象特許がどのセクションで論じられているか事前には分からないため)
            sections = {
                "101条(有用性等)": doc.get("sections.section101RejectionText"),
                "102条(新規性)": doc.get("sections.section102RejectionText"),
                "103条(非自明性)": doc.get("sections.section103RejectionText"),
                "112条(記載要件)": doc.get("sections.section112RejectionText"),
                "詳細引用文": doc.get("sections.detailCitationText"),
            }
            for label, val in sections.items():
                text = val[0] if val and val[0] else ""
                if not text:
                    continue
                found_any_text = True
                mentions_target = mentions_target_patent(text, patent_id)
                hits = highlight_similarity_phrases(text)

                if mentions_target:
                    found_target_mention = True
                    print(f"\n  ✅ [{label}] 本文中に {patent_id} への言及あり "
                         f"(類似性表現: {hits or 'なし'})")
                    print(f"  ---\n  {text[:1500]}\n  ---")
                else:
                    print(f"\n  ⚪ [{label}] 本文あり ({len(text)}文字) だが "
                         f"{patent_id} への直接言及は見つからず (類似性表現: {hits or 'なし'})")

        if not found_any_text:
            print("  (本文が空、またはこの記録には拒絶セクションなし)")
        elif not found_target_mention:
            print(f"\n  ⚠️  citationCategoryCode は {rec.get('citationCategoryCode')} だったが、"
                 f"取得できた本文中に {patent_id} への直接言及は見つからなかった "
                 f"(同一office action内の別引例が拒絶根拠になっている可能性。"
                 f"doc/調査記録_reject_デザイン類似性.md の再検証結果を参照)。")


if __name__ == "__main__":
    main()
