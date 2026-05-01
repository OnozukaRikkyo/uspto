import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

LOG_DIR         = Path("/mnt/eightthdd/uspto/log")
ADDED_CSV_PATH  = Path("/mnt/eightthdd/uspto/added.csv")
INDEX_PATH      = Path("/mnt/eightthdd/uspto/numpy_data/patent_ids.npy")

# D543613 → DESIGN_OFFSET + 543613 として int64 に収める
DESIGN_OFFSET = 10_000_000_000

CSV_COLUMNS = ['title', 'id', 'claim', 'date', 'class', 'class_search',
               'inv_country', 'no_figs', 'sheets', 'file_names', 'fig_desc', 'caption']
_UNFILLABLE_COLUMNS = [col for col in CSV_COLUMNS if col != 'id']


# ── ロガー ────────────────────────────────────────────────────────────────────

def setup_logger(name: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{name}_{datetime.now().strftime('%Y%m%d')}.log"
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ── HTTP エラーハンドリング ───────────────────────────────────────────────────

def check_status(response, context: str, logger: logging.Logger) -> None:
    """200以外は全て致命的エラーとしてログを残し即終了する。"""
    if response.status_code == 200:
        return
    msg = f"HTTP {response.status_code} | {context}"
    logger.critical(msg)
    sys.exit(f"\n致命的なAPIエラーが発生しました。プログラムを終了します。\n{msg}")


# ── ID 変換 ───────────────────────────────────────────────────────────────────

def id_to_int(patent_id: str) -> int | None:
    """
    正規化済み特許ID を int64 値に変換する。
      'D543613'  → 10_000_543_613
      '12345678' → 12_345_678
    """
    s = str(patent_id).strip().strip('.,')
    if s.upper().startswith('D') and s[1:].isdigit():
        return DESIGN_OFFSET + int(s[1:])
    if s.isdigit():
        return int(s)
    return None


def _normalize_id(raw_id: str) -> str | None:
    """'D0543613' → 'D543613', 数値文字列はそのまま正規化, それ以外 → None"""
    s = str(raw_id).strip().strip('.,')
    if s.upper().startswith('D') and s[1:].isdigit():
        return 'D' + str(int(s[1:]))
    if s.isdigit():
        return str(int(s))
    return None


def parse_cited_id(cited_str: str) -> str | None:
    """
    'US D543613 S' などから正規化済みID文字列を抽出する。
    スペース区切りで各トークンを試し、最初にヒットしたものを返す。
    """
    if not cited_str:
        return None
    for token in str(cited_str).strip().split():
        nid = _normalize_id(token)
        if nid:
            return nid
    return None


# ── 高速検索: KnownIds ────────────────────────────────────────────────────────

class KnownIds:
    """
    ソート済み numpy int64 配列 + added.csv 用 Python set による高速 ID 検索。

    numpy 配列 (np.searchsorted) : O(log n) — data/配下の全 CSV を格納
    Python set                   : O(1)      — 実行中に追記した added.csv 分
    """

    def __init__(self, arr: np.ndarray, added: set[int]):
        self._arr   = arr    # ソート済み int64 配列
        self._added = added  # added.csv 由来 + 実行中追記分

    def __contains__(self, patent_id: str) -> bool:
        n = id_to_int(patent_id)
        if n is None:
            return False
        if n in self._added:
            return True
        idx = np.searchsorted(self._arr, n)
        return int(idx) < len(self._arr) and self._arr[idx] == n

    def add(self, patent_id: str) -> None:
        """added.csv に追記したIDを実行内重複防止のため登録する。"""
        n = id_to_int(patent_id)
        if n is not None:
            self._added.add(n)


def load_known_ids(_data_dir: str = "") -> KnownIds:
    """
    numpy インデックスファイルから KnownIds を生成して返す。
    インデックスがなければ build_id_index.py を先に実行するよう案内して終了する。
    added.csv が存在すれば Python set に読み込む。
    """
    if not INDEX_PATH.exists():
        sys.exit(
            f"\n[ERROR] インデックスファイルが見つかりません: {INDEX_PATH}\n"
            f"先に build_id_index.py を実行してください。\n"
        )

    arr = np.load(INDEX_PATH)  # ソート済み int64 配列

    added: set[int] = set()
    if ADDED_CSV_PATH.exists():
        try:
            df = pd.read_csv(ADDED_CSV_PATH, usecols=['id'])
            for raw_id in df['id'].dropna():
                n = id_to_int(str(_normalize_id(str(raw_id)) or ''))
                if n is not None:
                    added.add(n)
        except Exception:
            pass

    return KnownIds(arr, added)


# ── cited チェック & added.csv 追記 ──────────────────────────────────────────

def check_and_register_cited(doc: dict, known_ids: KnownIds | None) -> None:
    """
    doc['citedDocumentIdentifier'] が known_ids に存在するか確認する。
    - 存在する → 何もしない
    - 存在しない → added.csv に追記し known_ids を更新（実行内重複防止）
    埋められない列は print で出力する。
    """
    if known_ids is None:
        return

    cited_str = doc.get("citedDocumentIdentifier", "")
    patent_id = parse_cited_id(cited_str)
    if not patent_id:
        return
    if patent_id in known_ids:
        return

    row = {col: '' for col in CSV_COLUMNS}
    row['id'] = patent_id

    print(f"  [added.csv] 新規特許 {patent_id} を追記 | 埋められない列: {', '.join(_UNFILLABLE_COLUMNS)}")

    new_df = pd.DataFrame([row])[CSV_COLUMNS]
    new_df.to_csv(
        ADDED_CSV_PATH,
        mode='a' if ADDED_CSV_PATH.exists() else 'w',
        header=not ADDED_CSV_PATH.exists(),
        index=False,
        encoding='utf-8',
    )

    known_ids.add(patent_id)