import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

LOG_DIR         = Path("/mnt/eightthdd/uspto/log")
ADDED_CSV_PATH  = Path("/mnt/eightthdd/uspto/added.csv")
INDEX_DIR       = Path("/mnt/eightthdd/uspto/numpy_data")

PATENT_IDS_PATH  = INDEX_DIR / "patent_ids.npy"
PATENT_META_PATH = INDEX_DIR / "patent_meta.npy"
FILE_LIST_PATH   = INDEX_DIR / "file_list.txt"

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
    正規化済み特許IDをint64値に変換する。
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
    """'D0543613' → 'D543613', 数値文字列は正規化, それ以外 → None"""
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
    ソート済み numpy int64 配列 + メタデータ配列 + added.csv 用 dict による高速ID検索。

    _arr  : shape(N,)   int64  ソート済み特許ID  → np.searchsorted で O(log n)
    _meta : shape(N,2)  int32  [file_idx, row_idx] （_arr と同順）
    _added: dict[int, list[int,int]]  {patent_int: [file_idx, row_idx]}
            added.csv 由来 + 実行中追記分。row_idx は追記時点で確定する。
    """

    def __init__(
        self,
        arr:            np.ndarray,
        meta:           np.ndarray,
        added:          dict,
        added_file_idx: int,
    ):
        self._arr           = arr
        self._meta          = meta
        self._added         = added          # {int: [file_idx, row_idx]}
        self._added_file_idx = added_file_idx  # added.csv のファイルインデックス

    def __contains__(self, patent_id: str) -> bool:
        n = id_to_int(patent_id)
        if n is None:
            return False
        if n in self._added:
            return True
        idx = np.searchsorted(self._arr, n)
        return int(idx) < len(self._arr) and self._arr[idx] == n

    def __len__(self) -> int:
        return len(self._arr) + len(self._added)

    def add(self, patent_id: str, row: int = 0) -> None:
        """
        patent_id を _added に登録する。
        row: added.csv への 0 ベース行インデックス（デフォルト 0）。
        check_and_register_cited から呼ぶ場合は実際の行番号を渡す。
        """
        n = id_to_int(patent_id)
        if n is not None:
            self._added[n] = [self._added_file_idx, row]

    def update_row(self, patent_id: str, row: int) -> None:
        """
        _added に登録済みエントリの行番号を後から更新する。
        add() を row=0 で呼んだ後、実際の書き込み行が確定したタイミングで使う。
        """
        n = id_to_int(patent_id)
        if n is not None and n in self._added:
            self._added[n][1] = row

    def get_location(self, patent_id: str) -> tuple[int, int] | None:
        """
        (file_idx, row_idx) を返す。未知の場合は None。
        file_idx は file_list.txt の行番号、added.csv は added_file_idx。
        row_idx は各ファイル内の 0 ベースデータ行インデックス。
        """
        n = id_to_int(patent_id)
        if n is None:
            return None
        if n in self._added:
            return (self._added[n][0], self._added[n][1])
        idx = np.searchsorted(self._arr, n)
        if int(idx) < len(self._arr) and self._arr[idx] == n:
            return (int(self._meta[idx, 0]), int(self._meta[idx, 1]))
        return None


def load_known_ids(_data_dir: str = "") -> KnownIds:
    """
    numpy インデックスファイル群から KnownIds を生成して返す。
    ファイルが存在しない場合は build_id_index.py を実行するよう案内して終了する。
    added.csv が存在すれば dict に読み込む。
    """
    for path in [PATENT_IDS_PATH, PATENT_META_PATH, FILE_LIST_PATH]:
        if not path.exists():
            sys.exit(
                f"\n[ERROR] インデックスファイルが見つかりません: {path}\n"
                f"先に build_id_index.py を実行してください。\n"
            )

    arr  = np.load(PATENT_IDS_PATH)
    meta = np.load(PATENT_META_PATH)

    with open(FILE_LIST_PATH, encoding='utf-8') as f:
        file_list = [line.strip() for line in f if line.strip()]
    added_file_idx = len(file_list)  # added.csv は data/ CSVs の次のインデックス

    added: dict[int, list] = {}
    if ADDED_CSV_PATH.exists():
        try:
            df = pd.read_csv(ADDED_CSV_PATH, usecols=['id'])
            for row_idx, raw_id in df['id'].dropna().items():
                nid = _normalize_id(str(raw_id))
                if nid:
                    n = id_to_int(nid)
                    if n is not None:
                        added[n] = [added_file_idx, int(row_idx)]
        except Exception:
            pass

    return KnownIds(arr, meta, added, added_file_idx)


# ── added.csv 行カウント ──────────────────────────────────────────────────────

def _next_added_row() -> int:
    """added.csv に次に追記される 0 ベースの行インデックスを返す。"""
    if not ADDED_CSV_PATH.exists():
        return 0
    with open(ADDED_CSV_PATH, 'r', encoding='utf-8') as f:
        n = sum(1 for _ in f)
    return max(0, n - 1)  # ヘッダー行を除く


# ── cited チェック & added.csv 追記 ──────────────────────────────────────────

def check_and_register_cited(doc: dict, known_ids: KnownIds | None) -> None:
    """
    doc['citedDocumentIdentifier'] をチェックし、未知の特許は added.csv に追記する。
    追記と同時に実際の行番号を known_ids に登録する。
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

    row_idx = _next_added_row()

    row = {col: '' for col in CSV_COLUMNS}
    row['id'] = patent_id

    print(f"  [added.csv] 新規特許 {patent_id} を追記 (row={row_idx}) | "
          f"埋められない列: {', '.join(_UNFILLABLE_COLUMNS)}")

    new_df = pd.DataFrame([row])[CSV_COLUMNS]
    new_df.to_csv(
        ADDED_CSV_PATH,
        mode='a' if ADDED_CSV_PATH.exists() else 'w',
        header=not ADDED_CSV_PATH.exists(),
        index=False,
        encoding='utf-8',
    )

    known_ids.add(patent_id, row=row_idx)