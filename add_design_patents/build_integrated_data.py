#!/usr/bin/env python3
"""IMPACT 統合データ出力

項目1の差分データ(CSV/TIF/parquet)を既存 IMPACT マスタと統合し、
/mnt/eightthdd/impact/integrated_data/ 配下に統合済みデータ一式を出力する。
元ディレクトリ(マスタ・add_design_patent)には一切書き込まない。

設計書: integrated_data/doc/設計書_IMPACT統合データ出力.md

使い方:
    python3 build_integrated_data.py                # 統合 + 検証
    python3 build_integrated_data.py --verify-only  # 検証のみ
"""

import argparse
import ast
import hashlib
import os
import random
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

# ---- 入力(読み取り専用) ----
SRC_DIR = Path(os.environ.get("BID_SRC_DIR", "/mnt/eightthdd/impact/add_design_patent"))
MASTER_DIR = Path(os.environ.get("BID_MASTER_DIR", "/mnt/eightthdd/uspto/data"))
DIFF_CSV_DIR = SRC_DIR / "csv"
MISSING_PATENTS_PATH = SRC_DIR / "missing_patents.parquet"
IMAGE_MANIFEST_PATH = SRC_DIR / "image_manifest.parquet"
CITATIONS_PATH = SRC_DIR / "missing_citations.parquet"

# ---- 出力(書き込みはこの配下のみ) ----
OUT_DIR = Path(os.environ.get("BID_OUT_DIR", "/mnt/eightthdd/impact/integrated_data"))
OUT_DATA_DIR = OUT_DIR / "data"
OUT_IMAGES_DIR = OUT_DIR / "images"
OUT_DOC_DIR = OUT_DIR / "doc"
OUT_MANIFEST_PATH = OUT_DIR / "image_manifest.parquet"
OUT_CITATIONS_PATH = OUT_DIR / "citations.parquet"
INTEGRATION_REPORT = OUT_DOC_DIR / "統合結果レポート.txt"
VERIFY_REPORT = OUT_DOC_DIR / "検証レポート.txt"

EXPECTED_COLS = [
    "title", "id", "claim", "date", "class", "class_search",
    "inv_country", "no_figs", "sheets", "file_names", "fig_desc", "caption",
]
ID_RE = re.compile(r"^D\d{7}$")
HARDLINK_SAMPLE = 1000


def read_csv_raw(path: Path) -> pd.DataFrame:
    """セル文字列を一切変換せずに読む(型劣化防止)"""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def preflight() -> None:
    for p in (MASTER_DIR, DIFF_CSV_DIR, MISSING_PATENTS_PATH, IMAGE_MANIFEST_PATH):
        if not p.exists():
            sys.exit(f"ERROR: 入力が存在しません: {p}")
    for src in (SRC_DIR, MASTER_DIR):
        if OUT_DIR.resolve().is_relative_to(src.resolve()):
            sys.exit(f"ERROR: 出力先 {OUT_DIR} が入力 {src} の配下にあります")
    OUT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DOC_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- CSV 統合

def merge_csvs() -> tuple[dict, list]:
    master_years = {p.stem for p in MASTER_DIR.glob("*.csv")}
    diff_years = {p.stem for p in DIFF_CSV_DIR.glob("*.csv")}
    years = sorted(master_years | diff_years)
    results = {}
    errors = []

    for year in tqdm(years, desc="CSV統合"):
        master_path = MASTER_DIR / f"{year}.csv"
        diff_path = DIFF_CSV_DIR / f"{year}.csv"
        out_path = OUT_DATA_DIR / f"{year}.csv"

        if master_path.exists() and not diff_path.exists():
            # 差分なし → バイト同一コピー
            tmp = out_path.with_suffix(".csv.tmp")
            shutil.copy2(master_path, tmp)
            os.replace(tmp, out_path)
            n = sum(1 for _ in open(master_path, encoding="utf-8")) - 1
            results[year] = {"master": n, "diff": 0, "dropped_dup": 0, "out": n, "mode": "copy"}
            continue

        frames = []
        n_master = n_diff = 0
        if master_path.exists():
            mdf = read_csv_raw(master_path)
            if list(mdf.columns) != EXPECTED_COLS:
                sys.exit(f"ERROR: {master_path} の列構成が想定と不一致: {list(mdf.columns)}")
            n_master = len(mdf)
            frames.append(mdf)
        ddf = read_csv_raw(diff_path)
        if list(ddf.columns) != EXPECTED_COLS:
            sys.exit(f"ERROR: {diff_path} の列構成が想定と不一致: {list(ddf.columns)}")
        n_diff = len(ddf)

        dropped = 0
        if frames:
            dup_mask = ddf["id"].isin(set(frames[0]["id"]))
            dropped = int(dup_mask.sum())
            if dropped:
                errors.append(
                    f"{year}: マスタと差分で id 重複 {dropped} 件 → マスタ優先で差分側を除外: "
                    + ", ".join(ddf.loc[dup_mask, "id"].head(10))
                )
                ddf = ddf[~dup_mask]
        frames.append(ddf)

        merged = pd.concat(frames, ignore_index=True).sort_values("id").reset_index(drop=True)
        in_year_dup = merged["id"].duplicated().sum()
        if in_year_dup:
            errors.append(f"{year}: 統合後 CSV 内に id 重複 {in_year_dup} 件(要調査)")

        tmp = out_path.with_suffix(".csv.tmp")
        merged.to_csv(tmp, index=False)
        os.replace(tmp, out_path)
        results[year] = {"master": n_master, "diff": n_diff, "dropped_dup": dropped,
                         "out": len(merged), "mode": "merge"}
    return results, errors


# ---------------------------------------------------------------- TIF ハードリンク

def link_images() -> tuple[pd.DataFrame, dict, list]:
    manifest = pd.read_parquet(IMAGE_MANIFEST_PATH)
    stats = {"linked": 0, "skipped": 0, "repaired": 0, "missing_src": 0, "copy_fallback": 0}
    errors = []
    new_paths = []

    for row in tqdm(manifest.itertuples(index=False), total=len(manifest), desc="TIFリンク"):
        src = Path(row.folder_path)
        year = str(row.grant_date)[:4]
        dst = OUT_IMAGES_DIR / year / src.name
        new_paths.append(str(dst))

        if not src.is_dir():
            stats["missing_src"] += 1
            errors.append(f"元フォルダ欠損: {src}")
            continue

        src_files = {e.name for e in os.scandir(src) if e.is_file()}
        if dst.is_dir():
            dst_files = {e.name for e in os.scandir(dst) if e.is_file()}
            todo = src_files - dst_files
            if not todo:
                stats["skipped"] += 1
                continue
            stats["repaired"] += 1
        else:
            dst.mkdir(parents=True, exist_ok=True)
            todo = src_files

        for name in todo:
            s, d = src / name, dst / name
            try:
                os.link(s, d)
            except OSError:
                shutil.copy2(s, d)
                stats["copy_fallback"] += 1
        if todo is src_files:
            stats["linked"] += 1

    out = manifest.copy()
    out["folder_path"] = new_paths
    tmp = OUT_MANIFEST_PATH.with_suffix(".parquet.tmp")
    out.to_parquet(tmp, index=False)
    os.replace(tmp, OUT_MANIFEST_PATH)
    return out, stats, errors


# ---------------------------------------------------------------- citations

def integrate_citations() -> tuple[int | None, int]:
    if not CITATIONS_PATH.exists():
        return None, 0
    df = pd.read_parquet(CITATIONS_PATH)
    n_before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    tmp = OUT_CITATIONS_PATH.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, OUT_CITATIONS_PATH)
    return len(df), n_before - len(df)


# ---------------------------------------------------------------- 検証

def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify() -> list:
    """検証フェーズ。戻り値は不合格項目のリスト(空なら合格)"""
    failures = []
    lines = []

    def check(name: str, ok: bool, detail: str = ""):
        mark = "✓" if ok else "✗"
        lines.append(f"{mark} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(f"{name}: {detail}")

    missing = pd.read_parquet(MISSING_PATENTS_PATH)
    missing_ids = set(missing["patent_id"])
    diff_years = {p.stem for p in DIFF_CSV_DIR.glob("*.csv")}
    master_years = {p.stem for p in MASTER_DIR.glob("*.csv")}

    # 1-4: CSV 検証
    all_out_ids: set = set()
    diff_row_total = 0
    for year in tqdm(sorted(master_years | diff_years), desc="検証:CSV"):
        out_path = OUT_DATA_DIR / f"{year}.csv"
        if not out_path.exists():
            check(f"CSV {year} 存在", False, str(out_path))
            continue
        odf = read_csv_raw(out_path)

        n_master = n_diff = 0
        if year in master_years:
            n_master = len(read_csv_raw(MASTER_DIR / f"{year}.csv"))
        if year in diff_years:
            ddf = read_csv_raw(DIFF_CSV_DIR / f"{year}.csv")
            n_diff = len(ddf)
            diff_row_total += n_diff
            # 9: manifest−CSV 突合は後段でまとめて実施するため id→file_names を保持
        expected = n_master + n_diff
        check(f"CSV {year} 行数", len(odf) >= n_master and len(odf) <= expected,
              f"out={len(odf)} master={n_master} diff={n_diff}")
        dup = odf["id"].duplicated().sum()
        check(f"CSV {year} id 重複なし", dup == 0, f"{dup} 件重複")
        bad_id = (~odf["id"].str.match(ID_RE)).sum()
        check(f"CSV {year} id 形式", bad_id == 0, f"{bad_id} 件不正")
        if year in master_years and year not in diff_years:
            check(f"CSV {year} コピー同一性(MD5)",
                  md5(out_path) == md5(MASTER_DIR / f"{year}.csv"))
        all_out_ids.update(odf["id"])

    # withdrawn 等で取得不能と確定した特許(state/unfound.txt)は欠落を許容
    unfound_path = SRC_DIR / "state" / "unfound.txt"
    known_unfound = set()
    if unfound_path.exists():
        known_unfound = {t for t in unfound_path.read_text().split() if ID_RE.match(t)}
    not_in_out = missing_ids - all_out_ids
    unexplained = not_in_out - known_unfound
    if not_in_out & known_unfound:
        lines.append(f"- 許容欠落(unfound.txt 登録済み withdrawn 等): "
                     f"{len(not_in_out & known_unfound)} 件 {sorted(not_in_out & known_unfound)}")
    check("網羅性: missing_patents 全件が統合CSVに存在(unfound 除く)", len(unexplained) == 0,
          f"{len(unexplained)} 件欠落 例: {sorted(unexplained)[:5]}")

    # 6-8: 画像・マニフェスト検証
    check("image_manifest.parquet 存在", OUT_MANIFEST_PATH.exists())
    if OUT_MANIFEST_PATH.exists():
        man = pd.read_parquet(OUT_MANIFEST_PATH)
        bad_folder = 0
        bad_fileset = 0
        bad_rep = 0
        src_manifest = pd.read_parquet(IMAGE_MANIFEST_PATH)
        src_by_id = dict(zip(src_manifest["patent_id"], src_manifest["folder_path"]))
        for row in tqdm(man.itertuples(index=False), total=len(man), desc="検証:画像"):
            dst = Path(row.folder_path)
            if not dst.is_dir():
                bad_folder += 1
                continue
            dst_files = {e.name for e in os.scandir(dst) if e.is_file()}
            src = Path(src_by_id[row.patent_id])
            if src.is_dir():
                src_files = {e.name for e in os.scandir(src) if e.is_file()}
                if src_files != dst_files:
                    bad_fileset += 1
            if row.rep_file not in dst_files:
                bad_rep += 1
        check("画像: 出力フォルダ実在", bad_folder == 0, f"{bad_folder} 件欠損")
        check("画像: ファイル名集合一致", bad_fileset == 0, f"{bad_fileset} 件不一致")
        check("画像: 代表図 rep_file 実在", bad_rep == 0, f"{bad_rep} 件欠損")
        check("画像: マニフェスト行数 = 元マニフェスト行数", len(man) == len(src_manifest),
              f"{len(man)} vs {len(src_manifest)}")

        # 7: ハードリンク実証(無作為サンプル)
        rng = random.Random(42)
        sample = man.sample(min(HARDLINK_SAMPLE, len(man)), random_state=42)
        mismatch = 0
        checked = 0
        for row in sample.itertuples(index=False):
            dst = Path(row.folder_path) / row.rep_file
            src = Path(src_by_id[row.patent_id]) / row.rep_file
            if dst.exists() and src.exists():
                checked += 1
                if os.stat(dst).st_ino != os.stat(src).st_ino:
                    mismatch += 1
        check(f"画像: ハードリンク inode 一致(sample={checked})", mismatch == 0,
              f"{mismatch} 件不一致(コピーfallback分は許容範囲か確認)")

        # 9: manifest rep_file と CSV file_names[0] の突合(差分年のみ)
        rep_mismatch = 0
        for year in sorted(diff_years):
            odf = read_csv_raw(OUT_DATA_DIR / f"{year}.csv")
            man_y = man[man["grant_date"].astype(str).str[:4] == year]
            rep_by_id = dict(zip(man_y["patent_id"], man_y["rep_file"]))
            sub = odf[odf["id"].isin(rep_by_id)]
            for pid, fn in zip(sub["id"], sub["file_names"]):
                try:
                    first = ast.literal_eval(fn)[0]
                except (ValueError, SyntaxError, IndexError):
                    rep_mismatch += 1
                    continue
                if first != rep_by_id[pid]:
                    rep_mismatch += 1
        check("画像: rep_file = CSV file_names[0](差分年)", rep_mismatch == 0,
              f"{rep_mismatch} 件不一致")

    # citations
    if CITATIONS_PATH.exists():
        check("citations.parquet 存在", OUT_CITATIONS_PATH.exists())
    else:
        lines.append("- citations: 入力未生成のため未統合(pending)")

    VERIFY_REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(VERIFY_REPORT, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(f"検証レポート — {datetime.now().isoformat()}\n")
        f.write("=" * 70 + "\n\n")
        f.write("\n".join(lines) + "\n\n")
        f.write(f"判定: {'PASSED' if not failures else 'FAILED (' + str(len(failures)) + ' 項目)'}\n")
    print(f"検証レポート: {VERIFY_REPORT}")
    return failures


# ---------------------------------------------------------------- レポート

def write_report(csv_results: dict, csv_errors: list, img_stats: dict,
                 img_errors: list, citations_n, citations_dropped: int) -> None:
    with open(INTEGRATION_REPORT, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(f"統合結果レポート — {datetime.now().isoformat()}\n")
        f.write("=" * 70 + "\n\n")

        f.write("[CSV 統合]\n")
        t_master = sum(r["master"] for r in csv_results.values())
        t_diff = sum(r["diff"] for r in csv_results.values())
        t_out = sum(r["out"] for r in csv_results.values())
        t_drop = sum(r["dropped_dup"] for r in csv_results.values())
        for y in sorted(csv_results):
            r = csv_results[y]
            f.write(f"  {y}: master={r['master']:>7} diff={r['diff']:>6} "
                    f"dup除外={r['dropped_dup']} out={r['out']:>7} ({r['mode']})\n")
        f.write(f"  合計: master={t_master} diff={t_diff} dup除外={t_drop} out={t_out}\n")
        if csv_errors:
            f.write("  警告:\n")
            for e in csv_errors:
                f.write(f"    - {e}\n")

        f.write("\n[TIF ハードリンク]\n")
        for k, v in img_stats.items():
            f.write(f"  {k}: {v}\n")
        if img_errors:
            f.write(f"  エラー({len(img_errors)}件、先頭20件):\n")
            for e in img_errors[:20]:
                f.write(f"    - {e}\n")

        f.write("\n[citations]\n")
        if citations_n is None:
            f.write("  入力 missing_citations.parquet 未生成のため未統合(pending)。\n")
            f.write("  完成後に本スクリプトを再実行すると citations.parquet が生成される。\n")
        else:
            f.write(f"  統合済み: {citations_n} 行(重複除去 {citations_dropped} 行)\n")

        f.write(f"\n出力先: {OUT_DIR}\n")
    print(f"統合結果レポート: {INTEGRATION_REPORT}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify-only", action="store_true", help="検証のみ実行")
    args = ap.parse_args()

    preflight()

    if not args.verify_only:
        csv_results, csv_errors = merge_csvs()
        _, img_stats, img_errors = link_images()
        citations_n, citations_dropped = integrate_citations()
        write_report(csv_results, csv_errors, img_stats, img_errors,
                     citations_n, citations_dropped)

    failures = verify()
    if failures:
        print(f"\n✗ 検証 FAILED: {len(failures)} 項目", file=sys.stderr)
        for x in failures:
            print(f"  - {x}", file=sys.stderr)
        sys.exit(1)
    print("\n✓ 検証 PASSED: 統合データは正しく出力されました")


if __name__ == "__main__":
    main()
