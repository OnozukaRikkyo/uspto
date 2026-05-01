import logging
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("/mnt/eightthdd/uspto/log")


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


def check_status(response, context: str, logger: logging.Logger) -> None:
    """200以外は全て致命的エラーとしてログを残し即終了する。"""
    if response.status_code == 200:
        return
    msg = f"HTTP {response.status_code} | {context}"
    logger.critical(msg)
    sys.exit(f"\n致命的なAPIエラーが発生しました。プログラムを終了します。\n{msg}")