# -*- coding: utf-8 -*-
"""日志模块:同时输出到控制台、文件、GUI 回调"""
import logging
import sys
from datetime import datetime
from pathlib import Path

from core.config import ROOT, Config


class GUiLogHandler(logging.Handler):
    """把日志转发到 GUI(通过回调函数)"""

    def __init__(self):
        super().__init__()
        self.callback = None  # gui 注册: callback(str)

    def emit(self, record):
        if self.callback:
            try:
                self.callback(self.format(record))
            except Exception:
                pass


_gui_handler = GUiLogHandler()


def _build_file_handler() -> logging.Handler | None:
    try:
        cfg = Config.get()["log"]
    except Exception:
        return None
    if not cfg.get("save_to_file", True):
        return None
    log_dir = ROOT / cfg.get("dir", "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    filename = log_dir / f"{datetime.now():%Y%m%d}.log"
    fh = logging.FileHandler(filename, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    return fh


def setup_logging():
    root = logging.getLogger()
    if root.handlers:  # 防止重复初始化
        return
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    _gui_handler.setFormatter(fmt)
    root.addHandler(_gui_handler)

    fh = _build_file_handler()
    if fh:
        root.addHandler(fh)


def set_gui_callback(callback):
    """GUI 调用此函数接收日志"""
    _gui_handler.callback = callback


def get_logger(name: str = "fuck_DXSTJ") -> logging.Logger:
    return logging.getLogger(name)
