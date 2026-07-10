"""日志配置：info/debug 双文件轮转 + 终端输出。"""

from __future__ import annotations

import gzip
import logging
import logging.handlers
import os
import shutil
import sys
from pathlib import Path

from .config import LOG_DEBUG_DIR, LOG_DEBUG_FILE, LOG_INFO_DIR, LOG_INFO_FILE, LOG_KEEP, LOG_MAX_SIZE


def _gzip_namer(default_name: str) -> str:
    return default_name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    try:
        os.remove(source)
    except OSError as e:
        logging.getLogger().warning(f"日志轮转后删除源文件失败: {source} ({e})")


def _make_rotating_handler(path: Path, level: int) -> logging.handlers.RotatingFileHandler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_SIZE, backupCount=LOG_KEEP, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.rotator = _gzip_rotator
    handler.namer = _gzip_namer
    return handler


def setup_logging(console_level: str = "INFO") -> logging.StreamHandler:
    """配置双路日志（info + debug）+ 终端输出，返回终端 handler 供后续调整级别。"""
    LOG_INFO_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    info_handler = _make_rotating_handler(LOG_INFO_FILE, logging.INFO)
    info_handler.setFormatter(fmt)
    logger.addHandler(info_handler)

    debug_handler = _make_rotating_handler(LOG_DEBUG_FILE, logging.DEBUG)
    debug_handler.setFormatter(fmt)
    logger.addHandler(debug_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(getattr(logging, console_level, logging.INFO))
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return stream_handler
