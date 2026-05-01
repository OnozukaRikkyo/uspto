#!/usr/bin/env python3
"""
全CSVのpatent IDをソート済みnumpy int64配列としてインデックス化する。
実行: python build_id_index.py

エンコード規則:
  D543613  → DESIGN_OFFSET + 543613  (10_000_543_613)
  12345678 → 12345678
"""
import glob
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR   = "/mnt/eightthdd/uspto/data"
OUTPUT_DIR = Path("/mnt/eightthdd/uspto/numpy_data")
OUTPUT_PATH = OUTPUT_DIR / "patent_ids.npy"

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

    ids: set[int] = set()
    for csv_path in csv_files:
        df = pd.read_csv(csv_path, usecols=['id'])
        before = len(ids)
        for raw_id in df['id'].dropna():
            n = id_to_int(str(raw_id))
            if n is not None:
                ids.add(n)
        added = len(ids) - before
        print(f"  {Path(csv_path).name}: {len(df):>6,}行  (+{added:,}件)")

    arr = np.array(sorted(ids), dtype=np.int64)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    np.save(OUTPUT_PATH, arr)
    print(f"\n✅ インデックス構築完了: {len(arr):,}件 → {OUTPUT_PATH}")


if __name__ == "__main__":
    build_index()