import os
import time
import logging
import argparse
import httpx
from pathlib import Path
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

api_key = os.getenv("MY_API_KEY")
if not api_key:
    raise EnvironmentError(
        "APIキーが設定されていません。\n"
        ".envファイルに MY_API_KEY=あなたのAPIキー を記述してください。"
    )

parser = argparse.ArgumentParser(description="USPTO 特許文書ダウンローダー")
parser.add_argument("--year", type=int, default=2023, help="対象年 (デフォルト: 2023)")
parser.add_argument(
    "--mode",
    choices=["skip", "overwrite"],
    default="skip",
    help="既存ファイルの扱い: skip=スキップ(デフォルト), overwrite=上書き",
)
args = parser.parse_args()

TARGET_YEAR = args.year
OVERWRITE = args.mode == "overwrite"
OUTPUT_DIR = Path(f"/mnt/eightthdd/us_patent/patent/{TARGET_YEAR}")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://api.uspto.gov"
HEADERS = {"X-API-KEY": api_key, "Accept": "application/json"}
PAGE_SIZE = 25

MIME_TO_EXT = {
    "application/pdf":   "pdf",
    "application/xml":   "xml",
    "text/xml":          "xml",
    "image/tiff":        "tiff",
    "image/jpeg":        "jpg",
    "image/png":         "png",
    "application/zip":   "zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "pdf":               "pdf",
    "xml":               "xml",
}

# tqdm.write() に転送するハンドラ（プログレスバーを壊さずログ出力）
class TqdmHandler(logging.Handler):
    def emit(self, record):
        tqdm.write(self.format(record))

handler = TqdmHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)


def search_applications(client: httpx.Client, year: int):
    query = f"applicationMetaData.filingDate:[{year}-01-01 TO {year}-12-31]"
    offset = 0
    total = None
    app_bar = None
    try:
        while True:
            resp = client.post(
                f"{BASE_URL}/api/v1/patent/applications/search",
                json={"q": query, "pagination": {"offset": offset, "limit": PAGE_SIZE}},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if total is None:
                total = data.get("count", 0)
                logger.info(f"{year}年の出願件数: {total:,}  モード: {args.mode}")
                app_bar = tqdm(
                    total=total, unit="件", desc="出願",
                    position=0, leave=True, dynamic_ncols=True,
                )
            items = data.get("patentFileWrapperDataBag", [])
            if not items:
                break
            for item in items:
                yield item, app_bar
            offset += PAGE_SIZE
            if offset >= total:
                break
            time.sleep(0.5)
    finally:
        if app_bar:
            app_bar.close()


def get_documents(client: httpx.Client, app_num: str):
    resp = client.get(
        f"{BASE_URL}/api/v1/patent/applications/{app_num}/documents",
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("documentBag", [])


def download_file(client: httpx.Client, url: str, dest: Path):
    with client.stream("GET", url, timeout=60, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        with (
            open(dest, "wb") as f,
            tqdm(
                total=total or None,
                unit="B", unit_scale=True, unit_divisor=1024,
                desc=dest.name[:40],
                position=1, leave=False, dynamic_ncols=True,
            ) as bar,
        ):
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)
                bar.update(len(chunk))


with httpx.Client(headers=HEADERS) as client:
    for app, app_bar in search_applications(client, TARGET_YEAR):
        app_num = app.get("applicationNumberText") or app.get("applicationNumber")
        if not app_num:
            app_bar.update(1)
            continue

        app_dir = OUTPUT_DIR / app_num
        app_dir.mkdir(parents=True, exist_ok=True)

        try:
            docs = get_documents(client, app_num)
            for doc in docs:
                code = doc.get("documentCode", "UNKNOWN")
                doc_id = doc.get("documentIdentifier", "000")

                for opt in doc.get("downloadOptionBag", []):
                    url = opt.get("downloadUrl")
                    if not url:
                        continue

                    mime = opt.get("mimeTypeIdentifier", "")
                    ext = MIME_TO_EXT.get(mime.lower(), "bin")
                    filename = f"{doc_id}_{code}.{ext}"
                    dest = app_dir / filename

                    if dest.exists() and not OVERWRITE:
                        continue

                    download_file(client, url, dest)
                    time.sleep(0.3)

        except Exception as e:
            logger.error(f"エラー: 出願番号 {app_num} - {e}")

        app_bar.update(1)
        app_bar.set_postfix_str(app_num, refresh=True)
        time.sleep(0.5)

logger.info("完了")
