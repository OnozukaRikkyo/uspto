#!/usr/bin/env python3
"""
全CSVのpatent IDをソート済みnumpy int64配列としてインデックス化する。
メタデータ（ファイルインデックス・行番号）も合わせて保存する。
実行: python build_id_index.py

エンコード規則:
  D543613  → DESIGN_OFFSET + 543613  (10_000_543_613)
  12345678 → 12345678

出力ファイル:
  patent_ids.npy   shape(N,)   dtype int64  -- ソート済み特許ID
  patent_meta.npy  shape(N,2)  dtype int32  -- [file_idx, row_idx]
  file_list.txt                             -- インデックス→ファイルパスの対応
"""
import glob
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR   = "/mnt/eightthdd/uspto/data"
OUTPUT_DIR = Path("/mnt/eightthdd/uspto/numpy_data")

PATENT_IDS_PATH  = OUTPUT_DIR / "patent_ids.npy"
PATENT_META_PATH = OUTPUT_DIR / "patent_meta.npy"
FILE_LIST_PATH   = OUTPUT_DIR / "file_list.txt"

DESIGN_OFFSET = 10_000_000_000


def id_to_int(raw_id: str) -> int | None:
    s = str(raw_id).strip()
    if s.upper().startswith('D') and s[1:].isdigit():
        return DESIGN_OFFSET + int(s[1:])
    if s.isdigit():
        return int(s)
    return None


def build_index():
    csv_files = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    if not csv_files:
        print(f"[ERROR] CSVファイルが見つかりません: {DATA_DIR}")
        return

    print(f"対象CSVファイル: {len(csv_files)}件\n")

    # {patent_int: (file_idx, row_idx)} 重複は最初の出現を優先
    seen: dict[int, tuple[int, int]] = {}

    for file_idx, csv_path in enumerate(csv_files):
        df = pd.read_csv(csv_path, usecols=['id'])
        count = 0
        for row_idx, raw_id in df['id'].dropna().items():
            n = id_to_int(str(raw_id))
            if n is not None and n not in seen:
                seen[n] = (file_idx, int(row_idx))
                count += 1
        print(f"  [{file_idx:2d}] {Path(csv_path).name}: {len(df):>6,}行  (+{count:,}件)")

    # patent_int でソートして numpy 配列に変換
    sorted_items = sorted(seen.items())
    ids  = np.array([item[0]         for item in sorted_items], dtype=np.int64)
    meta = np.array([list(item[1])   for item in sorted_items], dtype=np.int32)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    np.save(PATENT_IDS_PATH,  ids)
    np.save(PATENT_META_PATH, meta)
    with open(FILE_LIST_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(csv_files))

    print(f"\n✅ インデックス構築完了: {len(ids):,}件")
    print(f"   {PATENT_IDS_PATH}   shape={ids.shape}  dtype={ids.dtype}")
    print(f"   {PATENT_META_PATH}  shape={meta.shape} dtype={meta.dtype}")
    print(f"   {FILE_LIST_PATH}    ({len(csv_files)}ファイル)")


if __name__ == "__main__":
    build_index()