#!/usr/bin/env python3
"""追加分(IMPACT以外)の会社別名 JSON の生成(会社名の名寄せ)

IMPACT版 /home/sonozuka/uspto/company_aliases/build_company_aliases.py のコピーを
追加取得分(項目1でダウンロードした IMPACT に無い意匠特許)専用に適応したもの。
方法・パラメータ・フィルタは IMPACT 版と完全に同一。差分は入出力のみ:
  - 対象特許 = add_design_patent/csv/*.csv(追加分マスタ 165,917件)
  - 会社名ソース = assignee_missing.parquet の disambig_assignee_organization のみ
    (IMPACT の patent_applicants.csv / current_owner は使わない)
  - 出力 = add_design_patent/company_aliases/

設計書: /mnt/eightthdd/impact/add_design_patent/doc/設計_追加分_会社名名寄せ.md
(方法の詳細は IMPACT 版設計書 integrated_data/doc/設計書_項目2_会社名名寄せ.md 参照)

段階A: 会社名テーブル構築(追加分 disambig_assignee_organization、
       正規化 + 表記ゆれの決定的統合。canonical = 一番最初に意匠を登録した会社名)
段階B: クラス別会社名リスト作成
段階C1: クラスごとに全会社名を Qwen に一括投入し同一会社候補グループを検出
段階C2: 候補グループを計画書の PROMPT_TEMPLATE + ツール(search_web/find_company)で裏付け検証
段階D: 検証済みグループをクラス別 company_aliases_D{n}.json に出力

実行(フォアグラウンド・tqdm・中断→再実行で途中から):
    source /home/sonozuka/multimodal/venv/bin/activate
    python3 build_company_aliases.py                # 全段階
    python3 build_company_aliases.py --limit-chunks 3 --limit-groups 5   # 動作確認
    python3 build_company_aliases.py --rebuild-only # 段階A/B/D のみ(GPU不要)
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

# ---- 入力(読み取り専用) ----
ASSIGNEE_MISSING = Path(os.environ.get(
    "CA_ASSIGNEE_MISSING", "/mnt/eightthdd/impact/add_design_patent/assignee_missing.parquet"))
DATA_DIR = Path(os.environ.get(
    "CA_DATA_DIR", "/mnt/eightthdd/impact/add_design_patent/csv"))

# ---- 出力 ----
OUT_DIR = Path(os.environ.get(
    "CA_OUT", "/mnt/eightthdd/impact/add_design_patent/company_aliases"))
STATE_DIR = OUT_DIR / "state"
DOC_DIR = Path(os.environ.get(
    "CA_DOC", "/mnt/eightthdd/impact/add_design_patent/doc"))
REPORT_PATH = DOC_DIR / "名寄せ結果レポート_追加分.txt"

MODEL_NAME = os.environ.get("CA_MODEL", "Qwen/Qwen3-4B")
VALID_CLASSES = set(range(1, 35)) | {99}
CHUNK_SIZE = 400          # C1: 1プロンプトに載せる最大会社数
MAX_TURNS = 8             # C2: エージェントループの最大ターン
MAX_CONSEC_FAIL = 10
WEB_SLEEP = 0.5

# 計画書 項目2 の PROMPT_TEMPLATE(そのまま)
PROMPT_TEMPLATE = """Company: {company}
Task: Find former names or well-known aliases of this company using
search_web. For each candidate alias, call find_company to check whether
it exists in our table. Output ONLY the aliases that exist in the table,
as JSON: {{"names": ["{company}", "<alias1>", ...], "source": "<URL>"}}.
The URL must be a search result that supports the alias. If nothing is
confirmed, output {{"names": [], "source": ""}}.
Do not include subsidiaries or similarly-named different companies."""

# C2用: 計画書の PROMPT_TEMPLATE を「C1候補の検証」向けに拡張したもの
# (候補リストを明示し、確認できた候補のみ出力させる。検証はコード側で実施)
C2_PROMPT_TEMPLATE = """Company: {company}
Candidate aliases from our table: {candidates}
Task: Determine which candidates refer to the SAME company as the one
above, using search_web for supporting evidence. You may call
find_company to check exact strings in our table. Output ONLY the
confirmed names that exist in our table, as JSON:
{{"names": ["{company}", "<confirmed candidate>", ...], "source": "<URL>"}}.
The URL must be a search result that supports the alias. If nothing is
confirmed, output {{"names": [], "source": ""}}.
Do not include subsidiaries or similarly-named different companies."""

C1_PROMPT = """You are deduplicating company names from a design-patent assignee table
(design class D{cls}). Below is a numbered list of company names.
Identify groups of names that refer to THE SAME company (spelling
variants, abbreviations, former names, legal-suffix differences).
Rules:
- Only group names you are CONFIDENT refer to the same company.
- Do NOT group subsidiaries, regional affiliates, or similarly-named
  different companies.
- Names that merely start with the same word(s) are usually DIFFERENT
  companies. e.g. "HUNTER DOUGLAS INC" and "HUNTER FAN COMPANY" are
  different; "HUAWEI DEVICE CO., LTD" and "HUAWEI TECHNOLOGIES CO., LTD"
  are different legal entities — do not group them.
- Good example: "INTERNATIONAL BUSINESS MACHINES" and
  "INTERNATIONAL BUSINESS MACHINES CORPORATION" are the same company.
- Use the exact strings from the list.
- Output ONLY JSON: [["NAME A", "NAME B"], ...]. If none, output [].

{names}"""


# ---------------------------------------------------------------- 正規化

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).upper()
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    s = re.sub(r"\s*,\s*", ", ", s)
    return s


def key(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm(s))


SUFFIX_TOKENS = {
    "INC", "LLC", "CO", "LTD", "CORP", "CORPORATION", "COMPANY", "LIMITED",
    "INCORPORATED", "GMBH", "AG", "SA", "SARL", "BV", "NV", "KK", "KG", "PLC",
    "LP", "LLP", "PTY", "SRL", "SPA", "AB", "AS", "OY", "THE", "OF", "AND",
}


def core_tokens(name: str) -> tuple:
    """法人格語尾・括弧修飾を除いた識別トークン列(語順維持)"""
    s = re.sub(r"\([^)]*\)", " ", norm(name))          # 括弧修飾(州名等)を除去
    toks = re.sub(r"[^A-Z0-9 ]", " ", s).split()
    return tuple(t for t in toks if t not in SUFFIX_TOKENS)


def plausible_pair(a: str, b: str) -> bool:
    """同一会社の表記ゆれ・略称として機械的に妥当か(C1誤検出の決定的フィルタ)。
    通すもの: コアトークン一致 / 包含(小さい側2語以上) / Jaccard>=0.5 /
    頭字語(片方が1語で、他方のコアトークン頭文字列の前方一致。例 IBM)。
    排除するもの: HUGE/HULU 型の無関係ペア、HUAWEI DEVICE/TECHNOLOGIES 型の別法人。
    境界ケースの最終判定は C2 の wiki 裏付け検証が担う。"""
    ta, tb = core_tokens(a), core_tokens(b)
    sa, sb = set(ta), set(tb)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    # 頭字語: IBM == INTERNATIONAL BUSINESS MACHINES の頭文字列
    for single, multi in ((ta, tb), (tb, ta)):
        if len(single) == 1 and len(multi) >= 2 and len(single[0]) >= 2:
            initials = "".join(t[0] for t in multi)
            if initials.startswith(single[0]) or single[0] == initials[:len(single[0])]:
                return True
    small, large = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if small <= large:
        return len(small) >= 2
    return len(sa & sb) / len(sa | sb) >= 0.5


def refine_group(names: list) -> tuple:
    """plausible_pair で連結な成分(サイズ2以上)だけ残す。
    返り値: (残ったサブグループのリスト, 除外された名前のリスト)"""
    names = sorted(set(names))
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if plausible_pair(a, b):
                parent[find(a)] = find(b)
    comps = defaultdict(list)
    for n in names:
        comps[find(n)].append(n)
    kept = [sorted(c) for c in comps.values() if len(c) >= 2]
    dropped = sorted(n for c in comps.values() if len(c) < 2 for n in c)
    return kept, dropped


def parse_design_classes(s: str) -> set:
    """class 列から意匠クラス番号集合を得る(設計書 §3 の規則)"""
    out = set()
    for part in str(s).split(","):
        p = part.strip()
        if not p.startswith("D"):
            continue
        body = p[1:]
        if not body:
            continue
        if len(body) == 4 and body[0] != " ":   # 空白脱落形(D2968 → D2/968)
            c = body[0]
        else:                                    # 2文字固定幅クラスフィールド
            c = body[:2]
        try:
            n = int(c)
        except ValueError:
            continue
        if n in VALID_CLASSES:
            out.add(n)
    return out


# ---------------------------------------------------------------- 段階A

def pad7(pid: str) -> str:
    s = str(pid).strip()
    if s.upper().startswith("D") and s[1:].isdigit():
        return "D" + s[1:].zfill(7)
    return s


def stage_a() -> dict:
    """会社名テーブル構築。返り値: {key: {canonical, first_date, n_patents, classes,
    variants, patents}} と補助辞書"""
    print("段階A: 会社名テーブル構築")
    id_cls, id_date = {}, {}
    for f in tqdm(sorted(DATA_DIR.glob("*.csv")), desc="A: 特許マスタ読込"):
        df = pd.read_csv(f, dtype=str, keep_default_na=False,
                         usecols=["id", "date", "class"])
        for i, d, c in zip(df["id"], df["date"], df["class"]):
            id_cls[i] = parse_design_classes(c)
            id_date[i] = d

    pairs = []  # (pid7, raw_name)
    am = pd.read_parquet(ASSIGNEE_MISSING).dropna(
        subset=["disambig_assignee_organization"])
    for pid, org in zip(am["patent_id"], am["disambig_assignee_organization"]):
        pairs.append((pad7(pid), org))

    comp = {}
    for pid, raw in tqdm(pairs, desc="A: 名寄せ(段階1)"):
        d = id_date.get(pid)
        if d is None:
            continue
        k = key(raw)
        if not k:
            continue
        e = comp.setdefault(k, {"first_date": "99999999", "first_raw": "",
                                "variants": set(), "patents": set(), "classes": set()})
        e["variants"].add(norm(raw))
        e["patents"].add(pid)
        e["classes"] |= id_cls.get(pid, set())
        if d < e["first_date"]:
            e["first_date"] = d
            e["first_raw"] = raw
    for k, e in comp.items():
        e["canonical"] = norm(e["first_raw"])   # 一番最初に意匠を登録した会社名

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = [{"canonical_name": e["canonical"], "key": k,
             "n_patents": len(e["patents"]), "first_grant_date": e["first_date"],
             "first_raw_name": e["first_raw"],
             "classes": sorted(e["classes"]), "variants": sorted(e["variants"])}
            for k, e in comp.items()]
    pd.DataFrame(rows).sort_values("canonical_name").to_parquet(
        OUT_DIR / "company_table.parquet", index=False)
    nm = [{"raw_norm": v, "canonical_name": e["canonical"]}
          for e in comp.values() for v in e["variants"]]
    pd.DataFrame(nm).drop_duplicates().sort_values("raw_norm").to_parquet(
        OUT_DIR / "normalization_map.parquet", index=False)
    print(f"  会社名テーブル: {len(comp)} 社(表記ゆれ統合前 {len(nm)} 表記)")
    return comp


# ---------------------------------------------------------------- 段階B

def stage_b(comp: dict) -> dict:
    """クラス番号 → ソート済み canonical 名リスト"""
    by_cls = defaultdict(set)
    for e in comp.values():
        for c in e["classes"]:
            by_cls[c].add(e["canonical"])
    return {c: sorted(v) for c, v in sorted(by_cls.items())}


# ---------------------------------------------------------------- Qwen ランナー

class QwenRunner:
    """transformers ベースの貪欲デコード実行器(温度0相当)。テストでは chat を差し替える"""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self._tok = None

    def _load(self):
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        print(f"モデルロード中: {self.model_name}")
        self._tok = AutoTokenizer.from_pretrained(self.model_name)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_name, dtype=torch.bfloat16, device_map="cuda")
        self._model.eval()

    def chat(self, messages: list, tools: list | None = None,
             max_new_tokens: int = 1024) -> str:
        self._load()
        import torch
        text = self._tok.apply_chat_template(
            messages, tools=tools, add_generation_prompt=True,
            tokenize=False, enable_thinking=False)
        inputs = self._tok(text, return_tensors="pt").to(self._model.device)
        with torch.no_grad():
            out = self._model.generate(
                **inputs, do_sample=False, max_new_tokens=max_new_tokens,
                pad_token_id=self._tok.eos_token_id)
        return self._tok.decode(out[0][inputs["input_ids"].shape[1]:],
                                skip_special_tokens=True)


# ---------------------------------------------------------------- ツール

class Tools:
    def __init__(self, canonical_names: list):
        self.names = canonical_names
        self.names_upper = [(n.upper(), n) for n in canonical_names]
        self.web_cache_path = STATE_DIR / "web_cache.json"
        self.web_cache = {}
        if self.web_cache_path.exists():
            self.web_cache = json.loads(self.web_cache_path.read_text())
        self.session = requests.Session()
        self.session.headers["User-Agent"] = \
            "IMPACT-design-patent-research/1.0 (sonozuka2@gmail.com)"

    SCHEMAS = [
        {"type": "function", "function": {
            "name": "search_web",
            "description": "Search Wikipedia and Wikidata for a company. "
                           "Returns a list of {title, url, snippet}.",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "find_company",
            "description": "Case-insensitive partial-match search in our company "
                           "name table. Returns matching company names (max 20).",
            "parameters": {"type": "object",
                           "properties": {"text": {"type": "string"}},
                           "required": ["text"]}}},
    ]

    def find_company(self, text: str) -> list:
        t = str(text).upper().strip()
        if not t:
            return []
        return [orig for up, orig in self.names_upper if t in up][:20]

    def search_web(self, query: str) -> list:
        q = str(query).strip()
        if q in self.web_cache:
            return self.web_cache[q]
        results = []
        r = self.session.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": q,
            "srlimit": 5, "format": "json"}, timeout=30)
        r.raise_for_status()
        for h in r.json().get("query", {}).get("search", []):
            title = h["title"]
            results.append({
                "title": title,
                "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
                "snippet": re.sub(r"<[^>]+>", "", h.get("snippet", ""))})
        time.sleep(WEB_SLEEP)
        r = self.session.get("https://www.wikidata.org/w/api.php", params={
            "action": "wbsearchentities", "search": q, "language": "en",
            "type": "item", "limit": 5, "format": "json"}, timeout=30)
        r.raise_for_status()
        for h in r.json().get("search", []):
            results.append({
                "title": h.get("label", h["id"]),
                "url": "https://www.wikidata.org/wiki/" + h["id"],
                "snippet": h.get("description", "")})
        time.sleep(WEB_SLEEP)
        self.web_cache[q] = results
        self.web_cache_path.write_text(json.dumps(self.web_cache, ensure_ascii=False))
        return results


TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def extract_json(text: str):
    """最後の JSON オブジェクト/配列を抽出(思考文が混ざっても拾えるように)"""
    for pat in (r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", r"\[.*\]"):
        m = list(re.finditer(pat, text, re.DOTALL))
        if m:
            try:
                return json.loads(m[-1].group(0))
            except json.JSONDecodeError:
                continue
    return None


def extract_groups(text: str) -> list:
    """C1出力からグループ配列を抽出。全体JSONが途中切断でも、
    完結している内側配列(["A","B",...])だけ救済する"""
    parsed = extract_json(text)
    if isinstance(parsed, list) and all(isinstance(g, list) for g in parsed):
        return parsed
    out = []
    for m in re.finditer(
            r'\[\s*"(?:[^"\\]|\\.)*"(?:\s*,\s*"(?:[^"\\]|\\.)*")+\s*\]', text):
        try:
            out.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    return out


def agent_run(runner: QwenRunner, tools: Tools, prompt: str,
              max_turns: int = MAX_TURNS) -> tuple:
    """PROMPT_TEMPLATE エージェントループ。返り値 (final_json, seen_urls, turns, raw)"""
    messages = [{"role": "user", "content": prompt}]
    seen_urls = set()
    raw_last = ""
    for turn in range(1, max_turns + 1):
        out = runner.chat(messages, tools=Tools.SCHEMAS)
        raw_last = out
        calls = TOOL_CALL_RE.findall(out)
        if not calls:
            return extract_json(out), seen_urls, turn, raw_last
        messages.append({"role": "assistant", "content": out})
        for c in calls:
            try:
                call = json.loads(c)
                name = call.get("name", "")
                args = call.get("arguments", {}) or {}
                if name == "search_web":
                    res = tools.search_web(args.get("query", ""))
                    seen_urls |= {x["url"] for x in res}
                elif name == "find_company":
                    res = tools.find_company(args.get("text", ""))
                else:
                    res = {"error": f"unknown tool {name}"}
            except Exception as e:  # ツール実行エラーはモデルに返す
                res = {"error": str(e)}
            messages.append({"role": "tool", "name": name if calls else "",
                             "content": json.dumps(res, ensure_ascii=False)})
    return None, seen_urls, max_turns, raw_last  # ターン上限


# ---------------------------------------------------------------- 再開ヘルパ

def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 段階C1

def stage_c1(runner: QwenRunner, by_cls: dict, limit_chunks: int | None) -> list:
    """クラス別一括候補検出。返り値: 候補グループ(canonical名のリスト)のリスト"""
    print("\n段階C1: クラス別一括候補検出")
    path = STATE_DIR / "c1_results.jsonl"
    done = {(r["cls"], r["chunk"]) for r in load_jsonl(path)}

    jobs = []
    for cls, names in by_cls.items():
        for ci in range(0, len(names), CHUNK_SIZE):
            jobs.append((cls, ci // CHUNK_SIZE, names[ci:ci + CHUNK_SIZE]))
    todo = [j for j in jobs if (j[0], j[1]) not in done]
    if limit_chunks is not None:
        todo = todo[:limit_chunks]
    print(f"  チャンク: 全{len(jobs)} 済{len(jobs)-len(todo)} 今回{len(todo)}")

    fails = 0
    for cls, chunk_idx, names in tqdm(todo, desc="C1 候補検出"):
        listing = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
        prompt = C1_PROMPT.format(cls=cls, names=listing)
        try:
            out = runner.chat([{"role": "user", "content": prompt}],
                              max_new_tokens=4096)
            parsed = extract_groups(out)
            groups, filtered = [], []
            nameset = set(names)
            for g in parsed:
                if (isinstance(g, list) and len(g) >= 2
                        and all(isinstance(x, str) and x in nameset for x in g)):
                    kept, dropped = refine_group(g)   # 決定的フィルタ
                    groups.extend(kept)
                    if dropped:
                        filtered.append(dropped)
            append_jsonl(path, {"cls": cls, "chunk": chunk_idx,
                                "n_names": len(names), "groups": groups,
                                "filtered_out": filtered,
                                "ts": datetime.now().isoformat()})
            fails = 0
        except Exception as e:
            print(f"\nC1 エラー (D{cls} chunk{chunk_idx}): {e}", file=sys.stderr)
            fails += 1
            if fails >= MAX_CONSEC_FAIL:
                sys.exit(f"ERROR: {MAX_CONSEC_FAIL} 回連続失敗のため停止")

    seen, cands = set(), []
    for r in load_jsonl(path):
        for g in r["groups"]:
            fz = frozenset(g)
            if fz not in seen:
                seen.add(fz)
                cands.append(sorted(g))
    print(f"  候補グループ(重複排除後): {len(cands)}")
    return cands


# ---------------------------------------------------------------- 段階C2

def stage_c2(runner: QwenRunner, tools: Tools, comp_by_name: dict,
             candidates: list, limit_groups: int | None) -> None:
    """候補グループを PROMPT_TEMPLATE + ツールで裏付け検証"""
    print("\n段階C2: 候補グループの裏付け検証")
    path = STATE_DIR / "c2_results.jsonl"
    done = {frozenset(r["candidate"]) for r in load_jsonl(path)}
    todo = [c for c in candidates if frozenset(c) not in done]
    if limit_groups is not None:
        todo = todo[:limit_groups]
    print(f"  候補: 全{len(candidates)} 済{len(candidates)-len(todo)} 今回{len(todo)}")

    fails = 0
    for cand in tqdm(todo, desc="C2 裏付け検証"):
        rep = min(cand, key=lambda n: comp_by_name[n]["first_date"])
        others = [n for n in cand if n != rep]
        prompt = C2_PROMPT_TEMPLATE.format(
            company=rep, candidates=json.dumps(others, ensure_ascii=False))
        try:
            final, seen_urls, turns, raw = agent_run(runner, tools, prompt)
            status, reason, names, source = classify_c2(
                final, seen_urls, comp_by_name, allowed=set(cand))
            append_jsonl(path, {
                "candidate": sorted(cand), "rep": rep, "status": status,
                "reason": reason, "names": names, "source": source,
                "turns": turns, "seen_urls": sorted(seen_urls),
                "raw": raw[-2000:], "ts": datetime.now().isoformat()})
            fails = 0
        except Exception as e:
            print(f"\nC2 エラー ({rep}): {e}", file=sys.stderr)
            fails += 1
            if fails >= MAX_CONSEC_FAIL:
                sys.exit(f"ERROR: {MAX_CONSEC_FAIL} 回連続失敗のため停止")


def classify_c2(final, seen_urls: set, comp_by_name: dict,
                allowed: set | None = None) -> tuple:
    """C2 出力の決定的検証(設計書 §5-C2-3)。
    allowed を与えた場合、採用名は候補集合内に限定する(候補外・非テーブル名は
    トリムして記録し、2名未満になれば確認なし)"""
    if not isinstance(final, dict) or "names" not in final:
        return "エラー", "JSON出力なし/形式不正", [], ""
    names = final.get("names") or []
    source = str(final.get("source") or "")
    if not isinstance(names, list) or len(names) <= 1:
        return "確認なし", "裏付けの取れた別名なし", [], ""
    trimmed = []
    if allowed is not None:
        keep = [n for n in names if isinstance(n, str) and n in comp_by_name
                and n in allowed]
        trimmed = [n for n in names if n not in keep]
        if len(keep) <= 1:
            return "確認なし", f"候補内で確認できた名前が1件以下(トリム: {trimmed[:3]})", [], ""
        names = keep
    if not all(isinstance(n, str) and n in comp_by_name for n in names):
        bad = [n for n in names if not (isinstance(n, str) and n in comp_by_name)]
        return "破棄", f"テーブルに実在しない名前: {bad[:3]}", [], ""
    if len(set(names)) != len(names):
        return "破棄", "names 内に重複", [], ""
    if not source.startswith(("http://", "https://")):
        return "破棄", f"source が URL でない: {source[:80]}", [], ""
    if source not in seen_urls:
        return "破棄", f"search_web が返していない URL(幻覚の疑い): {source[:80]}", [], ""
    classes = set.intersection(*(set(comp_by_name[n]["classes"]) for n in names))
    if not classes:
        return "破棄", "共通の意匠クラスなし", [], ""
    return "採用", "", names, source


# ---------------------------------------------------------------- 段階D

def stage_d(comp: dict) -> list:
    """検証済みグループの出力生成。返り値: 検証エラーのリスト(空なら合格)"""
    print("\n段階D: 出力生成・検証")
    comp_by_name = {e["canonical"]: e for e in comp.values()}
    failures = []

    groups = []  # (names[代表先頭], source, origin)
    for r in load_jsonl(STATE_DIR / "c2_results.jsonl"):
        if r["status"] != "採用":
            continue
        names = r["names"]
        rep = min(names, key=lambda n: comp_by_name[n]["first_date"])
        ordered = [rep] + sorted((n for n in names if n != rep),
                                 key=lambda n: comp_by_name[n]["first_date"])
        groups.append({"names": ordered, "source": r["source"], "origin": "auto"})

    manual_path = OUT_DIR / "company_aliases_manual.json"
    if not manual_path.exists():
        manual_path.write_text("[]\n")
    for g in json.loads(manual_path.read_text()):
        names, source = g.get("names") or [], str(g.get("source") or "")
        if len(names) < 2 or not source:
            failures.append(f"manual: 形式不正 {g}")
            continue
        missing = [n for n in names if n not in comp_by_name]
        if missing:
            failures.append(f"manual: テーブルにない名前 {missing}")
            continue
        groups.append({"names": names, "source": source, "origin": "manual"})

    # グループ間衝突: 同じ会社名が複数グループ → manual優先、auto同士は全破棄
    by_name = defaultdict(list)
    for i, g in enumerate(groups):
        for n in g["names"]:
            by_name[n].append(i)
    drop = set()
    for n, idxs in by_name.items():
        if len(idxs) <= 1:
            continue
        manual_idxs = [i for i in idxs if groups[i]["origin"] == "manual"]
        if len(manual_idxs) >= 2:
            failures.append(f"manual 同士で会社名が衝突: {n}(手動ファイルを修正してください)")
            continue
        keep = manual_idxs[0] if manual_idxs else None
        for i in idxs:
            if i != keep:
                drop.add(i)
        print(f"  衝突: {n} → {'manual優先' if keep is not None else '全破棄'}")
    final_groups = [g for i, g in enumerate(groups) if i not in drop]

    # クラス別ファイル
    by_cls_file = defaultdict(list)
    dropped_nocls = 0
    for g in final_groups:
        classes = set.intersection(*(set(comp_by_name[n]["classes"])
                                     for n in g["names"]))
        if not classes:
            dropped_nocls += 1
            continue
        g["classes"] = sorted(classes)
        for c in sorted(classes):
            by_cls_file[c].append({"names": g["names"], "source": g["source"]})

    for old in OUT_DIR.glob("company_aliases_D*.json"):
        old.unlink()
    for c, gs in sorted(by_cls_file.items()):
        (OUT_DIR / f"company_aliases_D{c}.json").write_text(
            json.dumps(gs, ensure_ascii=False, indent=1) + "\n")
    (OUT_DIR / "company_aliases.json").write_text(
        json.dumps([{k: g[k] for k in ("names", "source", "origin", "classes")}
                    for g in final_groups if "classes" in g],
                   ensure_ascii=False, indent=1) + "\n")

    # query_log.csv(C2 全件)
    rows = [{"rep": r["rep"], "candidate": " | ".join(r["candidate"]),
             "status": r["status"], "reason": r["reason"],
             "names": " | ".join(r["names"]), "source": r["source"]}
            for r in load_jsonl(STATE_DIR / "c2_results.jsonl")]
    pd.DataFrame(rows).to_csv(OUT_DIR / "query_log.csv", index=False)

    # 検証(項目3 の検査を先取り)
    for c, gs in by_cls_file.items():
        seen = set()
        for g in gs:
            for n in g["names"]:
                if n in seen:
                    failures.append(f"D{c}: {n} が複数グループに出現")
                seen.add(n)
                if c not in comp_by_name[n]["classes"]:
                    failures.append(f"D{c}: {n} はこのクラスに出願していない")
            if g["names"][0] != min(g["names"],
                                    key=lambda n: comp_by_name[n]["first_date"]):
                failures.append(f"D{c}: 先頭が最古登録の会社名でない: {g['names']}")

    print(f"  採用グループ: {len(final_groups)}(クラス共通なし破棄 {dropped_nocls}、"
          f"衝突破棄 {len(drop)})→ クラスファイル {len(by_cls_file)} 本")
    return failures


def write_report(comp: dict, failures: list) -> None:
    c1 = load_jsonl(STATE_DIR / "c1_results.jsonl")
    c2 = load_jsonl(STATE_DIR / "c2_results.jsonl")
    status_counts = defaultdict(int)
    for r in c2:
        status_counts[r["status"]] += 1
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(f"名寄せ結果レポート(項目2) — {datetime.now().isoformat()}\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"会社名テーブル: {len(comp)} 社\n")
        f.write(f"C1 処理済みチャンク: {len(c1)}\n")
        f.write(f"C1 候補グループ(延べ): {sum(len(r['groups']) for r in c1)}\n")
        f.write(f"C2 判定済み: {len(c2)}\n")
        for s in ("採用", "確認なし", "破棄", "エラー"):
            f.write(f"  {s}: {status_counts.get(s, 0)}\n")
        f.write("\n採用グループ:\n")
        for r in c2:
            if r["status"] == "採用":
                f.write(f"  {' | '.join(r['names'])}\n    source: {r['source']}\n")
        f.write("\n破棄理由の内訳:\n")
        reasons = defaultdict(int)
        for r in c2:
            if r["status"] == "破棄":
                reasons[r["reason"].split(":")[0]] += 1
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            f.write(f"  {k}: {v}\n")
        f.write(f"\n検証: {'PASSED' if not failures else 'FAILED'}\n")
        for x in failures:
            f.write(f"  ✗ {x}\n")
    print(f"レポート: {REPORT_PATH}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild-only", action="store_true",
                    help="段階A/B/D のみ(GPU/API 照会なし)")
    ap.add_argument("--limit-chunks", type=int, default=None,
                    help="C1 のチャンク数を制限(動作確認用)")
    ap.add_argument("--limit-groups", type=int, default=None,
                    help="C2 の候補グループ数を制限(動作確認用)")
    ap.add_argument("--model", default=MODEL_NAME)
    args = ap.parse_args()

    for p in (ASSIGNEE_MISSING, DATA_DIR):
        if not p.exists():
            sys.exit(f"ERROR: 入力が存在しません: {p}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DOC_DIR.mkdir(parents=True, exist_ok=True)

    comp = stage_a()
    by_cls = stage_b(comp)
    comp_by_name = {e["canonical"]: e for e in comp.values()}

    if not args.rebuild_only:
        runner = QwenRunner(args.model)
        cands = stage_c1(runner, by_cls, args.limit_chunks)
        tools = Tools(sorted(comp_by_name))
        stage_c2(runner, tools, comp_by_name, cands, args.limit_groups)

    failures = stage_d(comp)
    write_report(comp, failures)
    if failures:
        print(f"\n✗ 検証 FAILED: {len(failures)} 項目", file=sys.stderr)
        sys.exit(1)
    print("\n✓ 完了")


if __name__ == "__main__":
    main()
