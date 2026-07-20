"""
add_design_patents 共通モジュール

IMPACT に不足している 2007 年以降登録の意匠特許を補完する 3 プログラム
(list_missing_patents.py / extract_grant_fulltext.py / fetch_citations.py)の共通処理。

データソースはすべて USPTO Open Data Portal (api.uspto.gov、X-API-KEY 必須)。
詳細は /mnt/eightthdd/impact/add_design_patent/doc/調査記録_データソース.md を参照。
"""
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

# ── パス設定 ──────────────────────────────────────────────────────────────────
DATA_ROOT   = Path("/mnt/eightthdd/impact/add_design_patent")
BULK_DIR    = DATA_ROOT / "bulk"       # ODP バルクテーブル (g_patent 等)
ARCHIVE_DIR = DATA_ROOT / "archives"   # PTGRDT 週次 tar (処理後に削除)
CSV_DIR     = DATA_ROOT / "csv"        # 年別 CSV (IMPACT と同一スキーマ)
IMAGES_DIR  = DATA_ROOT / "images"     # images/{year}/USDxxxxxxx-YYYYMMDD/
STATE_DIR   = DATA_ROOT / "state"      # 再開用の処理済みログ・キャッシュ
LOG_DIR     = DATA_ROOT / "log"

MISSING_PARQUET  = DATA_ROOT / "missing_patents.parquet"
MANIFEST_PARQUET = DATA_ROOT / "image_manifest.parquet"
ASSIGNEE_PARQUET = DATA_ROOT / "assignee_missing.parquet"
CITATIONS_PARQUET = DATA_ROOT / "missing_citations.parquet"

MASTER_DATA_DIR = Path("/mnt/eightthdd/uspto/data")   # IMPACT 由来マスタ CSV

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# ── API 設定 ──────────────────────────────────────────────────────────────────
API_BASE = "https://api.uspto.gov"

# IMPACT 年別 CSV のカラム (12 列。caption は空欄で出力する)
CSV_COLUMNS = ['title', 'id', 'claim', 'date', 'class', 'class_search',
               'inv_country', 'no_figs', 'sheets', 'file_names', 'fig_desc', 'caption']

# 代表図タイプ判定 (image_index.py / image_vector*.py の既存規則と同一)
PERSP_RE = re.compile(r'\bperspective\b', re.IGNORECASE)
FRONT_RE = re.compile(r'\bfront\s+(view|elevation|elevational|plan)\b', re.IGNORECASE)

IMPACT_ID_RE = re.compile(r'^D\d{7}$')   # マスタの形式: D+7桁ゼロ埋め (例 D0908314)


def detect_image_type(fig_desc_list: list[str]) -> str:
    """fig_desc リストから代表図タイプを判定する (既存規則をそのまま適用)。"""
    for desc in fig_desc_list:
        if PERSP_RE.search(str(desc)):
            return "perspective"
    for desc in fig_desc_list:
        if FRONT_RE.search(str(desc)):
            return "front"
    return "overview"


# ── ID 変換 ───────────────────────────────────────────────────────────────────

def to_impact_id(raw: str) -> str | None:
    """'D908314' / 'D0908314' → 'D0908314' (D+7桁ゼロ埋め、IMPACT マスタ形式)"""
    s = str(raw).strip().upper()
    if s.startswith('D') and s[1:].isdigit() and len(s[1:]) <= 7:
        return 'D' + s[1:].zfill(7)
    return None


def to_api_id(raw: str) -> str | None:
    """'D0908314' → 'D908314' (先頭ゼロ除去、ODP API のクエリ用)"""
    s = str(raw).strip().upper()
    if s.startswith('D') and s[1:].isdigit():
        return 'D' + str(int(s[1:]))
    return None


def id_to_int(raw: str) -> int | None:
    """'D0908314' / 'D908314' → 908314 (集合演算用の整数キー)"""
    s = str(raw).strip().upper()
    if s.startswith('D') and s[1:].isdigit():
        return int(s[1:])
    return None


# ── ロガー (tqdm と共存) ──────────────────────────────────────────────────────

class TqdmHandler(logging.Handler):
    def emit(self, record):
        tqdm.write(self.format(record))


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
    ch = TqdmHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ── API キー ──────────────────────────────────────────────────────────────────

def load_api_key() -> str:
    load_dotenv(ENV_FILE)
    key = os.getenv("MY_API_KEY")
    if not key:
        sys.exit(f"API キーが見つかりません。{ENV_FILE} に MY_API_KEY を設定してください。")
    return key


def api_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})
    return s


# ── HTTP リクエスト (プロセス内リトライなし) ─────────────────────────────────
# 方針 (doc/api_error_handling.md および既存 fetch_citations.py の流儀に統一。
#       doc/調査記録_APIレート制限.md も参照。2026-07-20: サーバー負荷軽減のため
#       5xx もプロセス内リトライを廃止しリトライなし一発勝負に変更):
#   400/401/403/404 など認証・URL 起因の 4xx → 致命的エラーとして即終了
#   429 (レート制限/週次クォータ超過)     → リトライせず即終了
#     (ODP バルクダウンロードは週次クォータ超過で 7 日間ロックアウトされるため、
#      リトライで叩き続けるとロックアウトを悪化させるだけで無意味)
#   5xx (サーバー側障害)・通信エラー        → リトライせず None を返す。
#     呼び出し側でこの1件をスキップし、次回のスクリプト再実行時に
#     未処理分として再試行される (state/ のチェックポイントで再開可能)。
#     プロセス内で同じ相手に間を置かず叩き直すと負荷軽減にならないため。

_FATAL_CODES = {400, 401, 403, 404}
_RATE_LIMIT_CODE = 429


def _exit_rate_limited(response: requests.Response, context: str, logger: logging.Logger):
    """
    429 応答時にリトライは行わず即終了する。ただしサーバーが返す Retry-After ヘッダーは
    (存在すれば) print/log するだけしておく — 実際の待機には使わない。
    ODP の Retry-After は通常の一時的レート制限では短時間 (数十秒程度) のことが多いようだが、
    バルクダウンロードの週次クォータ超過時にどう振る舞うかは未確認のため、値を見て
    今後の対応を判断する材料として記録する (doc/調査記録_APIレート制限.md 参照)。
    """
    retry_after = response.headers.get("Retry-After")
    print(f"\n⚠️  HTTP 429 (レート制限)。Retry-After ヘッダー: {retry_after!r} "
         f"(リトライはせず終了します)")
    msg = (f"HTTP {response.status_code} (レート制限/週次クォータ超過) | {context} | "
          f"Retry-After={retry_after!r} | {response.text[:300]}")
    logger.critical(msg)
    sys.exit(
        f"\nAPI レート制限 (週次クォータ) を超えました。リトライせず終了します。\n{msg}\n"
        f"ODP のバルクダウンロードは超過後 7 日間ロックアウトされます。時間をおいて再実行してください"
        f"(処理済み分は state/ にチェックポイントされているため途中から再開できます)。\n"
    )


def request_with_retry(session: requests.Session, method: str, url: str,
                       logger: logging.Logger, context: str = "",
                       **kwargs) -> requests.Response | None:
    """
    1 回だけリクエストを送る (関数名は既存呼び出し元との互換のため据え置くが、
    プロセス内リトライは行わない)。429/4xx は即終了、5xx/通信エラーは
    None を返して呼び出し側にスキップさせる。
    """
    try:
        r = session.request(method, url, **kwargs)
    except requests.exceptions.RequestException as e:
        logger.error(f"通信エラー (リトライなし・スキップ): {e} | {context}")
        return None
    if r.status_code == 200:
        return r
    if r.status_code == _RATE_LIMIT_CODE:
        _exit_rate_limited(r, context, logger)
    if r.status_code in _FATAL_CODES:
        msg = f"HTTP {r.status_code} | {context} | {r.text[:300]}"
        logger.critical(msg)
        sys.exit(f"\n致命的な API エラーです。終了します。\n{msg}")
    logger.error(f"HTTP {r.status_code} (5xx等、リトライなし・スキップ) | {context} | {r.text[:300]}")
    return None


# ── バルクファイルのダウンロード (Range 再開 + プログレスバー) ────────────────

def download_file(session: requests.Session, url: str, dest: Path,
                  logger: logging.Logger, desc: str | None = None) -> bool:
    """
    url を dest にダウンロードする。
    - dest が既に存在すればスキップ (True を返す)
    - 中断された .part があれば Range ヘッダで続きから再開
    - 成功時のみ .part → dest にリネーム
    """
    if dest.exists():
        logger.info(f"既存ファイルを使用: {dest}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    pos = part.stat().st_size if part.exists() else 0

    headers = {"Range": f"bytes={pos}-"} if pos else {}
    context = f"download {url}"
    try:
        r = session.get(url, headers=headers, stream=True, timeout=120)
    except requests.exceptions.RequestException as e:
        logger.error(f"通信エラー: {e} | {context}")
        return False

    if pos and r.status_code == 200:      # サーバが Range 非対応 → 最初から
        pos = 0
    elif pos and r.status_code == 416:    # 既に全量取得済み
        part.rename(dest)
        return True
    elif r.status_code not in (200, 206):
        if r.status_code == _RATE_LIMIT_CODE:
            _exit_rate_limited(r, context, logger)
        if r.status_code in _FATAL_CODES:
            logger.error(f"HTTP {r.status_code} | {context}")
            return False
        logger.warning(f"HTTP {r.status_code} | {context}")
        return False

    total = int(r.headers.get("content-length", 0)) + pos
    mode = "ab" if pos else "wb"
    with open(part, mode) as f, tqdm(
        total=total or None, initial=pos, unit="B", unit_scale=True,
        unit_divisor=1024, desc=desc or dest.name, dynamic_ncols=True,
    ) as bar:
        try:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                bar.update(len(chunk))
        except requests.exceptions.RequestException as e:
            logger.error(f"ダウンロード中断: {e} | {context} (再実行で続きから再開)")
            return False

    part.rename(dest)
    logger.info(f"ダウンロード完了: {dest} ({dest.stat().st_size:,} bytes)")
    return True


# ── ディレクトリ初期化 ────────────────────────────────────────────────────────

def ensure_dirs():
    for d in (BULK_DIR, ARCHIVE_DIR, CSV_DIR, IMAGES_DIR, STATE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
