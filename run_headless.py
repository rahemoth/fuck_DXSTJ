# -*- coding: utf-8 -*-
"""headless 运行器:复刻 GUI 的 Worker(Executor.run),直接跑真实做题主循环。
用于在缩小窗口下复现"选项错选"并采集日志。Ctrl+C 或跑完自动结束。"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.config import load_config
from core.log import setup_logging, get_logger
from core.pipeline.executor import Executor, ExecutorEvent


def main():
    setup_logging()
    cfg = load_config()
    log = get_logger()
    ex = Executor(cfg, emit=lambda e: log.info(f"[事件] {e.kind}: {e.data}"))
    t = threading.Thread(target=ex.run, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(timeout=1.0)
    except KeyboardInterrupt:
        ex.stop()
        t.join(timeout=10)


if __name__ == "__main__":
    main()
