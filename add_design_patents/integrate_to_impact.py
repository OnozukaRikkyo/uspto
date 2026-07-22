#!/usr/bin/env python3
"""
項目1.2: IMPACT データベースへの統合

項目1の差分データを既存IMPACTマスタに統合し、新規・更新データを確定する。
"""

import sys
import os
from pathlib import Path
from datetime import datetime
import shutil
import json

import pandas as pd
import numpy as np
from tqdm import tqdm

# 設定
DATA_DIR = Path("/mnt/eightthdd/impact/add_design_patent")
DOC_DIR = DATA_DIR / "doc"
CSV_DIR = DATA_DIR / "csv"
IMAGE_MANIFEST_PATH = DATA_DIR / "image_manifest.parquet"
CITATIONS_PATH = DATA_DIR / "missing_citations.parquet"
TIF_SRC_DIR = DATA_DIR / "images"

EXISTING_MASTER_DIR = Path("/mnt/eightthdd/uspto/data")
EXISTING_TIF_DIR = Path("/mnt/eightthdd/uspto/patent_tif")

BACKUP_DIR = DATA_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
UPDATED_CSV_DIR = DATA_DIR / "updated_csv"
INTEGRATION_REPORT = DOC_DIR / "実装_統合レポート_項目1.2.txt"


def backup_existing_masters() -> None:
    """既存マスタのバックアップを作成"""
    print("Creating backup of existing masters...")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # 既存CSVのバックアップ
    for csv_file in EXISTING_MASTER_DIR.glob("*.csv"):
        shutil.copy2(csv_file, BACKUP_DIR / csv_file.name)
        print(f"  Backed up: {csv_file.name}")

    print(f"✓ Backup created at {BACKUP_DIR}")


def merge_csv_with_existing() -> dict:
    """差分CSVと既存マスタを統合"""
    print("\nMerging CSVs with existing masters...")

    UPDATED_CSV_DIR.mkdir(parents=True, exist_ok=True)
    merge_results = {}
    errors = []

    # 全年のCSVを処理
    csv_files = sorted(CSV_DIR.glob("*.csv"))

    for csv_file in tqdm(csv_files, desc="CSV merge"):
        year = csv_file.stem

        # 差分CSV
        diff_df = pd.read_csv(csv_file)

        # 既存マスタ
        existing_csv = EXISTING_MASTER_DIR / f"{year}.csv"
        if existing_csv.exists():
            existing_df = pd.read_csv(existing_csv)
        else:
            existing_df = pd.DataFrame()

        # 統合
        if len(existing_df) > 0:
            # 重複チェック
            duplicates = diff_df[diff_df["id"].isin(existing_df["id"])]
            if len(duplicates) > 0:
                errors.append(f"Year {year}: {len(duplicates)} duplicate records with existing master")

            # 統合（差分追加）
            merged_df = pd.concat([existing_df, diff_df], ignore_index=True)
        else:
            merged_df = diff_df

        # ソート（patent_id昇順）
        merged_df = merged_df.sort_values("id").reset_index(drop=True)

        # 保存
        output_path = UPDATED_CSV_DIR / f"{year}.csv"
        merged_df.to_csv(output_path, index=False)

        merge_results[year] = {
            "existing": len(existing_df),
            "new": len(diff_df),
            "merged": len(merged_df),
        }
        print(f"  {year}: existing={len(existing_df)}, new={len(diff_df)}, merged={len(merged_df)}")

    if errors:
        report_errors(errors, "CSV merge")
        if any("duplicate" in e for e in errors):
            print("WARNING: Duplicates found. Continuing...", file=sys.stderr)

    return merge_results


def copy_tif_files(image_manifest_df: pd.DataFrame) -> dict:
    """TIFファイルを配置"""
    print("\nCopying TIF files...")

    if not TIF_SRC_DIR.exists():
        print(f"WARNING: Source TIF directory not found: {TIF_SRC_DIR}")
        return {}

    if not EXISTING_TIF_DIR.exists():
        print(f"ERROR: Destination TIF directory not found: {EXISTING_TIF_DIR}")
        sys.exit(1)

    copy_results = {
        "copied": 0,
        "skipped": 0,
        "error": 0,
        "conflicts": [],
    }

    for idx, row in tqdm(image_manifest_df.iterrows(), total=len(image_manifest_df), desc="TIF copy"):
        src_folder = Path(row.get("folder_path", ""))
        if not src_folder.exists():
            copy_results["error"] += 1
            continue

        # 宛先フォルダ
        dst_folder = EXISTING_TIF_DIR / src_folder.name

        if dst_folder.exists():
            # 競合チェック
            copy_results["conflicts"].append(str(dst_folder))
            copy_results["skipped"] += 1
            continue

        # コピー
        try:
            shutil.copytree(src_folder, dst_folder)
            copy_results["copied"] += 1
        except Exception as e:
            copy_results["error"] += 1
            print(f"ERROR copying {src_folder}: {str(e)}", file=sys.stderr)

    print(f"✓ TIF copy: {copy_results['copied']} copied, {copy_results['skipped']} skipped")
    return copy_results


def consolidate_citations() -> int:
    """引用テーブルを統合"""
    print("\nProcessing citations...")

    if not CITATIONS_PATH.exists():
        print("WARNING: missing_citations.parquet not found (fetch_citations.py still running?)")
        return 0

    df = pd.read_parquet(CITATIONS_PATH)
    print(f"✓ Loaded {len(df)} citation records")

    # 統合テーブルとして保存
    output_path = DATA_DIR / "citations_integrated.parquet"
    df.to_parquet(output_path, index=False)
    print(f"  Saved to {output_path}")

    return len(df)


def generate_integration_report(merge_results: dict, copy_results: dict, citations_count: int) -> None:
    """統合レポートを生成"""

    with open(INTEGRATION_REPORT, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"Integration Report - {datetime.now().isoformat()}\n")
        f.write("=" * 80 + "\n\n")

        f.write("CSV INTEGRATION\n")
        f.write("-" * 40 + "\n")
        total_existing = sum(r["existing"] for r in merge_results.values())
        total_new = sum(r["new"] for r in merge_results.values())
        total_merged = sum(r["merged"] for r in merge_results.values())

        f.write(f"Total existing records: {total_existing}\n")
        f.write(f"Total new records: {total_new}\n")
        f.write(f"Total merged records: {total_merged}\n\n")

        for year in sorted(merge_results.keys()):
            r = merge_results[year]
            f.write(f"  {year}: {r['existing']} + {r['new']} = {r['merged']}\n")

        f.write("\nTIF FILE DEPLOYMENT\n")
        f.write("-" * 40 + "\n")
        f.write(f"Copied: {copy_results.get('copied', 0)}\n")
        f.write(f"Skipped (existing): {copy_results.get('skipped', 0)}\n")
        f.write(f"Errors: {copy_results.get('error', 0)}\n")

        if copy_results.get('conflicts'):
            f.write("\nConflicting folders (already exist):\n")
            for conflict in copy_results['conflicts'][:10]:
                f.write(f"  {conflict}\n")
            if len(copy_results['conflicts']) > 10:
                f.write(f"  ... and {len(copy_results['conflicts']) - 10} more\n")

        f.write("\nCITATION INTEGRATION\n")
        f.write("-" * 40 + "\n")
        f.write(f"Citation records processed: {citations_count}\n")

        f.write("\nOUTPUT FILES\n")
        f.write("-" * 40 + "\n")
        f.write(f"Updated CSVs: {UPDATED_CSV_DIR}\n")
        f.write(f"TIF deployment: {EXISTING_TIF_DIR}\n")
        f.write(f"Citations table: {DATA_DIR}/citations_integrated.parquet\n")

        f.write("\nNEXT STEPS\n")
        f.write("-" * 40 + "\n")
        f.write("1. Review integration_report.txt and conflicts\n")
        f.write("2. Verify updated CSV files in updated_csv/\n")
        f.write(f"3. If all looks good, run:\n")
        f.write(f"   cp {UPDATED_CSV_DIR}/*.csv {EXISTING_MASTER_DIR}/\n")
        f.write(f"4. Verify final state and archive backup\n")

    print(f"✓ Integration report saved to {INTEGRATION_REPORT}")


def verify_counts(merge_results: dict, total_new: int) -> bool:
    """統計情報の検証"""
    print("\nVerifying integration integrity...")

    csv_total = sum(r["merged"] for r in merge_results.values())
    expected_new = total_new

    print(f"Expected new records: {expected_new}")
    print(f"Actual merged increase: {csv_total - sum(r['existing'] for r in merge_results.values())}")

    if expected_new == (csv_total - sum(r["existing"] for r in merge_results.values())):
        print("✓ Record count verification passed")
        return True
    else:
        print("⚠ Record count mismatch - review required")
        return False


def main():
    print("Item 1.2: Integration to IMPACT Database\n")

    # ドキュメントディレクトリの確認
    DOC_DIR.mkdir(parents=True, exist_ok=True)

    # バックアップ作成
    backup_existing_masters()

    # CSV統合
    merge_results = merge_csv_with_existing()

    # TIFファイル配置
    copy_results = copy_tif_files(pd.read_parquet(IMAGE_MANIFEST_PATH))

    # 引用テーブル統合
    citations_count = consolidate_citations()

    # レポート生成
    generate_integration_report(merge_results, copy_results, citations_count)

    # 整合性検証
    total_new = sum(r["new"] for r in merge_results.values())
    verify_counts(merge_results, total_new)

    print("\n✓ Integration preparation complete")
    print(f"\nReview integration_report.txt at {INTEGRATION_REPORT}")
    print("When ready to apply, copy updated CSVs from updated_csv/ to the live directory.")


if __name__ == "__main__":
    main()
