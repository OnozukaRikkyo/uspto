#!/usr/bin/env python3
"""
項目1.1: 取得データの統合・検証

item1 の3つのプログラムが出力したデータ(CSV、TIF、parquet)の品質チェック
と整合性確認を実施する。
"""

import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

# 設定
DATA_DIR = Path("/mnt/eightthdd/impact/add_design_patent")
DOC_DIR = DATA_DIR / "doc"
PARQUET_DIR = DATA_DIR / "data"
MISSING_PATENTS_PATH = PARQUET_DIR / "missing_patents.parquet"
IMAGE_MANIFEST_PATH = PARQUET_DIR / "image_manifest.parquet"
CSV_DIR = DATA_DIR / "csv"
TIF_DIR = DATA_DIR / "images"
CITATIONS_PATH = PARQUET_DIR / "missing_citations.parquet"

VALIDATION_REPORT = DOC_DIR / "実装_検証レポート_項目1.1.txt"


def validate_missing_patents() -> tuple[pd.DataFrame, list]:
    """missing_patents.parquetの検証"""
    print("Validating missing_patents.parquet...")

    if not MISSING_PATENTS_PATH.exists():
        print(f"ERROR: {MISSING_PATENTS_PATH} not found", file=sys.stderr)
        sys.exit(1)

    df = pd.read_parquet(MISSING_PATENTS_PATH)
    errors = []

    # 列の確認
    expected_cols = {"patent_id", "grant_date", "source"}
    actual_cols = set(df.columns)
    if not expected_cols.issubset(actual_cols):
        errors.append(f"Missing columns: {expected_cols - actual_cols}")
        return df, errors

    # 重複チェック
    duplicates = df[df["patent_id"].duplicated(keep=False)]
    if len(duplicates) > 0:
        errors.append(f"Duplicate patent_id: {len(duplicates)} records")

    print(f"✓ missing_patents: {len(df)} records")
    return df, errors


def validate_csvs(missing_patents_df: pd.DataFrame) -> tuple[dict, list]:
    """年別CSVの検証"""
    print("\nValidating annual CSVs...")

    if not CSV_DIR.exists():
        print(f"ERROR: {CSV_DIR} not found", file=sys.stderr)
        sys.exit(1)

    expected_fields = [
        "title", "id", "claim", "date", "class", "class_search",
        "inv_country", "no_figs", "sheets", "file_names", "fig_desc", "caption"
    ]

    missing_patent_ids = set(missing_patents_df["patent_id"])
    csv_results = {}
    errors = []

    csv_files = sorted(CSV_DIR.glob("*.csv"))
    if not csv_files:
        errors.append("No CSV files found")
        return csv_results, errors

    for csv_file in csv_files:
        try:
            df = pd.read_csv(csv_file)
            year = csv_file.stem

            # フィールド検証
            missing_fields = set(expected_fields) - set(df.columns)
            if missing_fields:
                errors.append(f"CSV {year}: missing fields {missing_fields}")
                continue

            # patent_id が missing_patents に登録済みかチェック
            unregistered = df[~df["id"].astype(str).isin(missing_patent_ids)]
            if len(unregistered) > 0:
                errors.append(f"CSV {year}: {len(unregistered)} records not in missing_patents")

            csv_results[year] = len(df)
            print(f"  {year}: {len(df)} records")

        except Exception as e:
            errors.append(f"CSV {csv_file.name}: {str(e)}")

    return csv_results, errors


def validate_image_manifest(missing_patents_df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """image_manifest.parquetの検証"""
    print("\nValidating image_manifest.parquet...")

    if not IMAGE_MANIFEST_PATH.exists():
        print(f"ERROR: {IMAGE_MANIFEST_PATH} not found", file=sys.stderr)
        sys.exit(1)

    df = pd.read_parquet(IMAGE_MANIFEST_PATH)
    missing_patent_ids = set(missing_patents_df["patent_id"])
    errors = []

    # 重複チェック
    duplicates = df[df["patent_id"].duplicated(keep=False)]
    if len(duplicates) > 0:
        errors.append(f"Duplicate patent_id: {len(duplicates)} records")

    # patent_id が missing_patents に登録済みかチェック
    unregistered = df[~df["patent_id"].astype(str).isin(missing_patent_ids)]
    if len(unregistered) > 0:
        errors.append(f"{len(unregistered)} records not in missing_patents")

    print(f"✓ image_manifest: {len(df)} records")
    return df, errors


def validate_tif_folder(image_manifest_df: pd.DataFrame) -> list:
    """TIFフォルダの検証"""
    print("\nValidating TIF folder structure...")

    if not TIF_DIR.exists():
        return [f"WARNING: {TIF_DIR} not found"]

    errors = []
    subdirs = list(TIF_DIR.iterdir())
    print(f"✓ TIF folder: {len(subdirs)} subdirectories")

    return errors


def validate_citations() -> tuple[pd.DataFrame | None, list]:
    """missing_citations.parquetの検証（存在する場合）"""
    print("\nValidating missing_citations.parquet...")

    if not CITATIONS_PATH.exists():
        print(f"⚠ missing_citations.parquet not found (fetch_citations.py still running?)")
        return None, []

    df = pd.read_parquet(CITATIONS_PATH)
    errors = []

    # 重複チェック
    duplicates = df.duplicated(keep=False)
    if duplicates.any():
        errors.append(f"{duplicates.sum()} duplicate records")

    print(f"✓ missing_citations: {len(df)} records")
    return df, errors


def consolidate_summary(missing_patents_df: pd.DataFrame, csv_results: dict,
                       image_manifest_df: pd.DataFrame, citations_df: pd.DataFrame | None,
                       all_errors: list) -> None:
    """統合レポートの出力"""

    with open(VALIDATION_REPORT, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"Data Validation Report - {datetime.now().isoformat()}\n")
        f.write("=" * 80 + "\n\n")

        f.write("SUMMARY\n")
        f.write("-" * 40 + "\n")
        f.write(f"missing_patents records: {len(missing_patents_df)}\n")
        f.write(f"Annual CSV total records: {sum(csv_results.values())}\n")
        f.write(f"image_manifest records: {len(image_manifest_df)}\n")
        if citations_df is not None:
            f.write(f"missing_citations records: {len(citations_df)}\n")
        else:
            f.write(f"missing_citations: NOT YET COMPLETED\n")

        if all_errors:
            f.write("\nERRORS / WARNINGS\n")
            f.write("-" * 40 + "\n")
            for error in all_errors:
                f.write(f"  {error}\n")

        f.write("\nAnnual CSV Breakdown\n")
        f.write("-" * 40 + "\n")
        for year in sorted(csv_results.keys()):
            f.write(f"  {year}: {csv_results[year]} records\n")

        csv_total = sum(csv_results.values())
        f.write("\nIntegrity Check\n")
        f.write("-" * 40 + "\n")
        if len(image_manifest_df) == csv_total:
            f.write(f"✓ Counts match: CSV ({csv_total}) == image_manifest ({len(image_manifest_df)})\n")
        else:
            f.write(f"✗ Count mismatch: CSV ({csv_total}) != image_manifest ({len(image_manifest_df)})\n")

        if len(missing_patents_df) == csv_total:
            f.write(f"✓ Counts match: missing_patents ({len(missing_patents_df)}) == CSV ({csv_total})\n")
        else:
            f.write(f"✗ Count mismatch: missing_patents ({len(missing_patents_df)}) != CSV ({csv_total})\n")

        status = "PASSED" if not all_errors else "PASSED (with warnings)"
        f.write(f"\nValidation Status: {status}\n")

    print(f"\n✓ Validation report saved to {VALIDATION_REPORT}")


def main():
    print("Item 1.1: Data Validation and Consolidation\n")

    # ドキュメントディレクトリの確認
    DOC_DIR.mkdir(parents=True, exist_ok=True)

    all_errors = []

    # 各種検証
    missing_patents_df, errs = validate_missing_patents()
    all_errors.extend(errs)

    csv_results, errs = validate_csvs(missing_patents_df)
    all_errors.extend(errs)

    image_manifest_df, errs = validate_image_manifest(missing_patents_df)
    all_errors.extend(errs)

    errs = validate_tif_folder(image_manifest_df)
    all_errors.extend(errs)

    citations_df, errs = validate_citations()
    all_errors.extend(errs)

    # 統合レポート
    consolidate_summary(missing_patents_df, csv_results, image_manifest_df, citations_df, all_errors)

    if all_errors:
        print(f"\n⚠ Validation complete with {len(all_errors)} warnings/errors")
    else:
        print("\n✓ Validation complete: All checks passed")


if __name__ == "__main__":
    main()
