# -*- coding: utf-8 -*-
"""调试辅助:实测滚动一步页面实际移动的像素数"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from core.config import load_config
from core.controller.window import WindowCapture
from core.controller.input import InputController


def content_y_offset(a1, a2):
    """估算页面向下滚动的像素数:页面下滚时内容上移,
    在 a2 中向上下两个方向搜索 a1 顶部内容的新位置"""
    strip = a1[100:300, 300:900]
    best, best_off = 1e9, 0
    h, w = a2.shape
    for off in range(-400, 401):
        y1, y2 = 100 + off, 300 + off
        if y1 < 0 or y2 > h:
            continue
        cand = a2[y1:y2, 300:900]
        if cand.shape != strip.shape:
            continue
        d = float(np.abs(strip.astype(int) - cand.astype(int)).mean())
        if d < best:
            best, best_off = d, off
    return -best_off if best < 5 else None   # 内容上移off像素 = 页面下滚off像素


def main():
    cfg = load_config()
    w = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
    w.ensure_connected()
    inp = InputController(w, cfg["action"])
    for notches in (1, 1, 3):
        img1 = w.screenshot()
        a1 = np.asarray(img1.convert("L"))
        inp.scroll(notches)
        time.sleep(1.2)
        img2 = w.screenshot()
        a2 = np.asarray(img2.convert("L"))
        off = content_y_offset(a1, a2)
        print(f"滚动 {notches} 格 -> 页面下移 {off}px" if off is not None
              else f"滚动 {notches} 格 -> 页面未移动或匹配失败")


if __name__ == "__main__":
    main()
