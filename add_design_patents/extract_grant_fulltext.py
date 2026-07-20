#!/usr/bin/env python3
"""
extract_grant_fulltext.py — 全文アーカイブからの展開 (項目1-(2))

missing_patents.parquet に列挙された意匠特許を、BDSS 後継の ODP バルク製品
PTGRDT (Patent Grant Full Text Data with Embedded TIFF Images、週次 tar) から
取り出し、IMPACT と同一の 12 列年別 CSV・TIF フォルダ・manifest を生成する。

処理の流れ (週次アーカイブ単位):
  0. PTGRDT 製品のファイル一覧 API から、必要な日付範囲の実ファイル名を取得する。
     週次アーカイブは年代により拡張子が .tar と .ZIP に混在しており (同じ年内でも週によって
     異なる。2026-07-20 実測: 2007〜2010年台は大半が .ZIP、一部週だけ .tar 等、法則性なし)、
     ファイル名を "I{YYYYMMDD}.tar" と決め打ちすると存在しない URL になり、ODP がエラー時に
     返す HTML ページを誤ってアーカイブとしてダウンロードしてしまう (実際に発生した障害)。
     そのため必ず API 応答の fileDownloadURI / fileName をそのまま使う。
  1. 不足特許の登録日から必要な週を特定し、上記マッピングでファイル名/URLを引く
  2. アーカイブをダウンロード (中断時は Range で再開)。ダウンロード後、tar/zip として
     開けるか検証し (is_tarfile/is_zipfile)、開けなければ破損とみなして削除しスキップする
  3. アーカイブをディスクに全展開せず、メンバーを走査して DESIGN/*.ZIP のうち
     不足特許のみをメモリ上で展開 (.tar と .ZIP どちらのコンテナでも同じロジックで処理)
  4. XML を解析して年別 CSV に追記 (IMPACT の process_xml.py と同一の抽出規則。
     caption は生成せず空欄)。TIF+XML は images/{year}/USDxxxxxxx-YYYYMMDD/ に保存
  5. アーカイブ 1 本を処理し終えたら削除 (--keep-tar で保持)

再開: state/extracted.jsonl にある特許はスキップ。処理済み tar は再取得しない。
検証: 展開できなかった特許は state/unfound.txt / state/extract_failed.txt とログに記録。

譲受人: 11 フィールドに含まれないため、PVGPATDIS の g_assignee_disambiguated.tsv.zip
から不足特許分のみ抽出して assignee_missing.parquet に保存する (--skip-assignees で省略)。

使い方:
  source /home/sonozuka/network_fig/venv/bin/activate
  python3 extract_grant_fulltext.py                  # 全件
  python3 extract_grant_fulltext.py --limit-tars 1   # 動作確認 (tar 1本のみ)
  python3 extract_grant_fulltext.py --assignees-only # 譲受人抽出のみ
"""
import argparse
import csv
import io
import json
import re
import sys
import tarfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
from tqdm import tqdm

from common import (API_BASE, ARCHIVE_DIR, ASSIGNEE_PARQUET, BULK_DIR,
                    CSV_COLUMNS, CSV_DIR, IMAGES_DIR, MANIFEST_PARQUET,
                    MISSING_PARQUET, STATE_DIR, api_session, detect_image_type,
                    download_file, ensure_dirs, load_api_key, request_with_retry,
                    setup_logger, to_api_id)

EXTRACTED_JSONL = STATE_DIR / "extracted.jsonl"     # 1行1特許の処理記録 (manifest の元)
TARS_DONE_TXT   = STATE_DIR / "tars_done.txt"       # 走査完了した週 (日付) の記録
UNFOUND_TXT     = STATE_DIR / "unfound.txt"         # アーカイブ内に見つからなかった特許
FAILED_TXT      = STATE_DIR / "extract_failed.txt"  # XML 解析等に失敗した特許

G_ASSIGNEE_URL = f"{API_BASE}/api/v1/datasets/products/files/PVGPATDIS/g_assignee_disambiguated.tsv.zip"
G_ASSIGNEE_ZIP = BULK_DIR / "g_assignee_disambiguated.tsv.zip"

# PTGRDT の週次アーカイブ本体のみに一致 (I{YYYYMMDD}.tar / .zip)。
# -SUPP.ZIP や .dtd 等の補助ファイルは除外する。
_PTGRDT_NAME_RE = re.compile(r'^I(\d{8})\.(tar|zip)$', re.IGNORECASE)

logger = setup_logger("extract_grant_fulltext")


# ── PTGRDT 実ファイル名の解決 ─────────────────────────────────────────────────

def fetch_ptgrdt_index(session, dates: set[str]) -> dict[str, dict]:
    """
    PTGRDT 製品のファイル一覧 API から、必要な日付範囲の実ファイル名/URLを取得する。
    週次アーカイブの拡張子は年代・週によって .tar / .ZIP が混在し法則性がないため、
    ファイル名を決め打ちせずここで解決した実際の fileName / fileDownloadURI を使う
    (doc/障害記録_20260720_tar拡張子.md 参照)。
    戻り値: {date8: {"filename": str, "url": str}}
    """
    if not dates:
        return {}
    d_min, d_max = min(dates), max(dates)
    from_date = f"{d_min[:4]}-{d_min[4:6]}-{d_min[6:]}"
    to_date = f"{d_max[:4]}-{d_max[4:6]}-{d_max[6:]}"
    url = (f"{API_BASE}/api/v1/datasets/products/PTGRDT"
          f"?fileDataFromDate={from_date}&fileDataToDate={to_date}&includeFiles=true")
    r = request_with_retry(session, "GET", url, logger, context="PTGRDT file index")
    if r is None:
        sys.exit("PTGRDT ファイル一覧の取得に失敗しました。再実行してください。")

    index: dict[str, dict] = {}
    for p in r.json().get("bulkDataProductBag", []):
        for f in p.get("productFileBag", {}).get("fileDataBag", []):
            name = f.get("fileName", "")
            m = _PTGRDT_NAME_RE.match(name)
            if not m:
                continue
            index[m.group(1)] = {"filename": name, "url": f.get("fileDownloadURI", "")}
    return index


# ── アーカイブ抽象化 (.tar / .ZIP どちらのトップレベルコンテナも同一ロジックで扱う) ──

class _TarView:
    def __init__(self, path: Path):
        self._tar = tarfile.open(path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._tar.close()

    def iter_names(self):
        for m in self._tar:
            if m.isfile():
                yield m.name

    def read(self, name: str) -> bytes:
        return self._tar.extractfile(name).read()


class _ZipView:
    def __init__(self, path: Path):
        self._zip = zipfile.ZipFile(path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._zip.close()

    def iter_names(self):
        for info in self._zip.infolist():
            if not info.is_dir():
                yield info.filename

    def read(self, name: str) -> bytes:
        return self._zip.read(name)


def is_valid_archive(path: Path) -> bool:
    """ダウンロードしたファイルが実際に tar/zip として開けるか検証する。"""
    return tarfile.is_tarfile(path) or zipfile.is_zipfile(path)


def _open_archive(path: Path):
    if tarfile.is_tarfile(path):
        return _TarView(path)
    return _ZipView(path)


# ── XML 解析 (IMPACT の process_xml.py と同一の抽出規則) ─────────────────────

def parse_grant_xml(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)

    patent_title = root.find('.//invention-title').text
    patent_id = root.find('.//doc-number').text
    claim = root.find('.//claim-text').text
    class_USPC = root.find('.//classification-national/main-classification').text
    class_USPC_fur = root.find('.//classification-national/further-classification')
    search = root.findall('.//main-classification')
    sheets = root.find('.//number-of-drawing-sheets').text
    search_list = [elem.text for elem in search]
    date = root.find('.//date').text
    no_figs = root.find('.//number-of-figures').text
    countries = ','.join([c.find('.//country').text for c in root.findall('.//inventor')
                          if c.find('.//country') is not None])
    file_names = [img.get('file') for img in root.findall('.//img')]

    fig_list = []
    count = 0
    for p in root.iter('p'):
        if count < int(no_figs):
            texts = list(p.itertext())
            element = ' '.join(text.strip() for text in texts)
            fig_list.append(element)
            count += 1

    appl_ref = root.find('.//application-reference//doc-number')

    return {
        'title': patent_title,
        'id': patent_id,
        'claim': claim,
        'date': date,
        'class': class_USPC + ',' + class_USPC_fur.text if class_USPC_fur is not None else class_USPC,
        'class_search': search_list,
        'inv_country': countries,
        'no_figs': no_figs,
        'sheets': sheets,
        'file_names': file_names,
        'fig_desc': fig_list,
        'caption': '',   # LLaVA 生成物は使用しないため空欄
        '_appl_number': appl_ref.text if appl_ref is not None else '',
    }


# ── 状態ファイル ──────────────────────────────────────────────────────────────

def load_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}


def append_line(path: Path, line: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_extracted_ids() -> set[str]:
    ids = set()
    if EXTRACTED_JSONL.exists():
        with open(EXTRACTED_JSONL, encoding="utf-8") as f:
            for ln in f:
                try:
                    ids.add(json.loads(ln)["patent_id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return ids


# ── CSV 出力 ──────────────────────────────────────────────────────────────────

def append_csv_row(year: str, row: dict):
    """IMPACT と同一スキーマの年別 CSV に 1 行追記する (リスト列は str(list) 表現)。"""
    path = CSV_DIR / f"{year}.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row[k] for k in CSV_COLUMNS})


def dedup_year_csvs(years: set[str]):
    """クラッシュ時の二重追記を除去する (id 重複行の後勝ちを排除)。"""
    for y in years:
        path = CSV_DIR / f"{y}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        if df["id"].duplicated().any():
            n = df["id"].duplicated().sum()
            df.drop_duplicates(subset=["id"], keep="first").to_csv(path, index=False)
            logger.warning(f"{path.name}: 重複 {n} 行を除去")


# ── アーカイブ 1 本の処理 (.tar / .ZIP どちらでも共通処理) ────────────────────

def process_archive(archive_path: Path, pending: dict[str, str]) -> tuple[set[str], set[str]]:
    """
    pending: {patent_id(D+7桁): grant_date(YYYYMMDD)} このアーカイブに入っているはずの未処理特許
    戻り値: (展開成功 id 集合, 解析失敗 id 集合)
    """
    done: set[str] = set()
    failed: set[str] = set()
    bar = tqdm(total=len(pending), desc=f"{archive_path.name} 展開", unit="件", dynamic_ncols=True)
    scanned = 0
    with _open_archive(archive_path) as archive:
        for name in archive.iter_names():
            scanned += 1
            if scanned % 500 == 0:
                bar.set_postfix_str(f"走査 {scanned} メンバー")
            name_up = name.upper()
            if "/DESIGN/" not in name_up or not name_up.endswith(".ZIP"):
                continue
            stem = Path(name).stem                    # USD0939806-20220104
            if not stem.upper().startswith("USD"):
                continue
            pid = stem[2:].split("-")[0].upper()       # D0939806
            if pid not in pending or pid in done:
                continue
            try:
                zbytes = archive.read(name)
                ok = process_patent_zip(pid, stem, zbytes)
            except Exception as e:
                logger.error(f"{pid}: 展開失敗 ({name}): {e}")
                append_line(FAILED_TXT, f"{pid}\t{archive_path.name}\t{e}")
                failed.add(pid)
                bar.update(1)
                continue
            if ok:
                done.add(pid)
            else:
                failed.add(pid)
            bar.update(1)
            if len(done) + len(failed) == len(pending):
                break
    bar.close()
    return done, failed


def process_patent_zip(pid: str, stem: str, zbytes: bytes) -> bool:
    """zip (XML + TIF) をメモリ上で展開し、CSV 追記・画像保存・manifest 記録を行う。"""
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    xml_names = [n for n in zf.namelist() if n.upper().endswith(".XML")]
    if not xml_names:
        logger.error(f"{pid}: zip 内に XML がありません")
        append_line(FAILED_TXT, f"{pid}\tno_xml")
        return False

    try:
        row = parse_grant_xml(zf.read(xml_names[0]))
    except Exception as e:
        logger.error(f"{pid}: XML 解析失敗: {e}")
        append_line(FAILED_TXT, f"{pid}\txml_parse\t{e}")
        return False

    grant_date = row["date"]                     # YYYYMMDD
    year = grant_date[:4]

    # TIF + XML を IMPACT と同一の USDID-日付 フォルダ構成で保存
    out_dir = IMAGES_DIR / year / stem           # images/{year}/USD0939806-20220104/
    out_dir.mkdir(parents=True, exist_ok=True)
    for n in zf.namelist():
        base = Path(n).name
        if not base:
            continue
        (out_dir / base).write_bytes(zf.read(n))

    # 年別 CSV (12列、IMPACT スキーマ)
    append_csv_row(year, row)

    # manifest 記録 (代表図 = file_names[0]、タイプ判定は既存規則)
    rep_file = row["file_names"][0] if row["file_names"] else ""
    rec = {
        "patent_id": pid,
        "grant_date": grant_date,
        "folder_path": str(out_dir),
        "rep_file": rep_file,
        "image_type": detect_image_type(row["fig_desc"]),
        "n_files": len(zf.namelist()),
        "appl_number": row["_appl_number"],
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    append_line(EXTRACTED_JSONL, json.dumps(rec, ensure_ascii=False))
    return True


# ── manifest 再構築 ───────────────────────────────────────────────────────────

def rebuild_manifest():
    if not EXTRACTED_JSONL.exists():
        return
    recs = []
    with open(EXTRACTED_JSONL, encoding="utf-8") as f:
        for ln in f:
            try:
                recs.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    if recs:
        df = pd.DataFrame(recs).drop_duplicates(subset=["patent_id"], keep="first")
        df.to_parquet(MANIFEST_PARQUET, index=False)
        print(f"📦 manifest 更新: {MANIFEST_PARQUET} ({len(df):,} 件)")


# ── 譲受人抽出 ────────────────────────────────────────────────────────────────

def extract_assignees(session, missing_ids_api: set[str]):
    print("\n👥 譲受人テーブル (g_assignee_disambiguated) から不足特許分を抽出...")
    if not download_file(session, G_ASSIGNEE_URL, G_ASSIGNEE_ZIP, logger,
                         desc="g_assignee_disambiguated.tsv.zip"):
        logger.error("譲受人テーブルのダウンロードに失敗しました (再実行してください)")
        return
    chunks = []
    reader = pd.read_csv(G_ASSIGNEE_ZIP, sep="\t", dtype=str, compression="zip",
                         chunksize=1_000_000)
    for chunk in tqdm(reader, desc="譲受人抽出", unit="chunk", dynamic_ncols=True):
        hit = chunk[chunk["patent_id"].isin(missing_ids_api)]
        if len(hit):
            chunks.append(hit)
    if chunks:
        df = pd.concat(chunks, ignore_index=True)
    else:
        df = pd.DataFrame()
    df.to_parquet(ASSIGNEE_PARQUET, index=False)
    n_pat = df["patent_id"].nunique() if len(df) else 0
    print(f"✅ 譲受人保存: {ASSIGNEE_PARQUET} ({len(df):,} 行 / {n_pat:,} 特許)")
    logger.info(f"assignees rows={len(df)} patents={n_pat} / missing={len(missing_ids_api)}")


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="PTGRDT 週次 tar から不足意匠特許を展開")
    ap.add_argument("--limit-tars", type=int, default=0,
                    help="処理するアーカイブ本数の上限 (動作確認用。0=無制限で --max-archives-per-run"
                         " に従う。明示指定した場合はこちらが優先される)")
    ap.add_argument("--max-archives-per-run", type=int, default=25,
                    help="1回の実行で処理するアーカイブ本数の既定上限 (安全マージン、既定25本)。"
                         "ODP バルクダウンロードの週次クォータの正確な数値は非公開のため、"
                         "1回の実行で全251本規模を連続ダウンロードして未知の上限を超えないよう"
                         "自動的にここで一時停止し、続きは翌日以降の再実行に委ねる。0で無効化"
                         "(無制限。週次クォータの制約が判明するまでは非推奨)")
    ap.add_argument("--keep-tar", action="store_true", help="処理済み tar を削除しない")
    ap.add_argument("--skip-assignees", action="store_true", help="譲受人抽出を行わない")
    ap.add_argument("--assignees-only", action="store_true", help="譲受人抽出のみ実行")
    ap.add_argument("--tar-sleep", type=float, default=2.0,
                    help="週次 tar 1本ごとのダウンロード後の待機秒数 (ODP バルクダウンロードの"
                         "週次クォータ超過→7日ロックアウトを避けるためのサーバー負荷軽減。既定2.0秒)")
    args = ap.parse_args()

    ensure_dirs()
    api_key = load_api_key()
    session = api_session(api_key)

    if not MISSING_PARQUET.exists():
        sys.exit(f"{MISSING_PARQUET} がありません。先に list_missing_patents.py を実行してください。")
    missing = pd.read_parquet(MISSING_PARQUET)
    missing["date8"] = missing["grant_date"].astype(str).str.replace("-", "").str[:8]
    missing_ids_api = {to_api_id(p) for p in missing["patent_id"]}

    if args.assignees_only:
        extract_assignees(session, missing_ids_api)
        return

    extracted = load_extracted_ids()
    unfound = {ln.split("\t")[0] for ln in load_lines(UNFOUND_TXT)}
    tars_done = load_lines(TARS_DONE_TXT)

    todo = missing[~missing["patent_id"].isin(extracted | unfound)]
    by_date: dict[str, dict[str, str]] = {}
    for pid, d8 in zip(todo["patent_id"], todo["date8"]):
        by_date.setdefault(d8, {})[pid] = d8
    dates = [d for d in sorted(by_date) if d not in tars_done]

    print(f"📋 不足特許: {len(missing):,} 件 / 展開済み: {len(extracted):,} / "
          f"未発見: {len(unfound):,} / 残り: {len(todo):,}")
    print(f"📅 対象 tar: {len(dates)} 本 (処理済み {len(tars_done)} 本はスキップ)")

    n_remaining_total = len(dates)
    auto_paced = False
    if args.limit_tars:
        dates = dates[:args.limit_tars]
        print(f"   --limit-tars={args.limit_tars} により今回分: {len(dates)} 本")
    elif args.max_archives_per_run and len(dates) > args.max_archives_per_run:
        dates = dates[:args.max_archives_per_run]
        auto_paced = True
        print(f"   ⏸️  安全マージン (--max-archives-per-run={args.max_archives_per_run}) "
             f"により今回分: {len(dates)} 本 (残り {n_remaining_total - len(dates)} 本は次回以降)")

    # 実ファイル名/URLを PTGRDT ファイル一覧 API で解決 (拡張子の .tar/.ZIP 混在に対応)
    print("🔎 PTGRDT の実ファイル名を解決中...")
    ptgrdt_index = fetch_ptgrdt_index(session, set(dates))
    n_missing_idx = sum(1 for d8 in dates if d8 not in ptgrdt_index)
    if n_missing_idx:
        logger.warning(f"PTGRDT に該当ファイルが見つからない週: {n_missing_idx} 件")

    touched_years: set[str] = set()
    total_done = total_failed = total_unfound = 0

    for i, d8 in enumerate(tqdm(dates, desc="週次アーカイブ", unit="本", dynamic_ncols=True), 1):
        pending = by_date[d8]
        year = d8[:4]

        info = ptgrdt_index.get(d8)
        if info is None:
            logger.error(f"{d8}: PTGRDT に該当する週次ファイルが見つかりません。スキップします。")
            for pid in sorted(pending):
                append_line(UNFOUND_TXT, f"{pid}\tNO_ARCHIVE_{d8}")
            append_line(TARS_DONE_TXT, d8)
            total_unfound += len(pending)
            continue

        archive_name = info["filename"]
        archive_path = ARCHIVE_DIR / archive_name
        url = info["url"]

        if not download_file(session, url, archive_path, logger, desc=archive_name):
            logger.error(f"{archive_name}: ダウンロード失敗。スキップして次へ (再実行で再試行)")
            if args.tar_sleep:
                time.sleep(args.tar_sleep)
            continue

        if not is_valid_archive(archive_path):
            logger.critical(f"{archive_name}: 有効な tar/zip として開けません "
                            f"(破損またはURL誤りの可能性)。削除してスキップします (再実行で再試行)。")
            archive_path.unlink(missing_ok=True)
            if args.tar_sleep:
                time.sleep(args.tar_sleep)
            continue

        dedup_year_csvs({year})   # 前回クラッシュ分の重複除去
        done, failed = process_archive(archive_path, pending)
        not_found = set(pending) - done - failed
        for pid in sorted(not_found):
            append_line(UNFOUND_TXT, f"{pid}\t{archive_name}")
            logger.warning(f"{pid}: {archive_name} 内に見つかりませんでした")

        append_line(TARS_DONE_TXT, d8)
        touched_years.add(year)
        total_done += len(done)
        total_failed += len(failed)
        total_unfound += len(not_found)
        tqdm.write(f"  {archive_name}: 成功 {len(done)} / 失敗 {len(failed)} / 未発見 {len(not_found)}")
        logger.info(f"{archive_name}: done={len(done)} failed={len(failed)} unfound={len(not_found)}")

        if not args.keep_tar:
            archive_path.unlink(missing_ok=True)   # 展開済みアーカイブは削除

        if i % 5 == 0:
            rebuild_manifest()

        if args.tar_sleep and i < len(dates):
            time.sleep(args.tar_sleep)   # ODP バルクダウンロードへの連続アクセスを避ける

    rebuild_manifest()
    print(f"\n📊 今回実行: 成功 {total_done:,} / 解析失敗 {total_failed:,} / 未発見 {total_unfound:,}")
    print(f"   失敗一覧: {FAILED_TXT}\n   未発見一覧: {UNFOUND_TXT}")

    n_left = n_remaining_total - len(dates)
    if n_left > 0:
        print(f"\n⏸️  今回はここまで (残り {n_left:,} 本)。譲受人抽出は全アーカイブ処理完了後にまとめて行うため、"
             f"今回はスキップします。")
        if auto_paced:
            print(f"   ODP バルクダウンロードの週次クォータの安全マージンのため、意図的に一時停止しています。\n"
                 f"   同じコマンド (python3 extract_grant_fulltext.py) を後で(できれば翌日以降)再実行すると"
                 f"続きから進みます。")
            sys.exit(75)   # EX_TEMPFAIL 相当: 一時停止であり致命的エラーではない
        print(f"   --limit-tars を指定したため打ち切りました。続きは同じコマンドを再実行してください。")
        return

    if not args.skip_assignees:
        extract_assignees(session, missing_ids_api)

    print("\n✅ 完了")


if __name__ == "__main__":
    main()
