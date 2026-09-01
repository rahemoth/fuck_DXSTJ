# -*- coding: utf-8 -*-
"""调试辅助:检测当前页面各选项的选中状态(蓝色圆圈/文字)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from core.config import load_config
from core.controller.window import WindowCapture
from core.vision.ocr import OcrEngine
from core.vision.locator import QuestionLocator


def main():
    cfg = load_config()
    w = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
    w.ensure_connected()
    img = w.screenshot()
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    ocr = OcrEngine(cfg["ocr"]["confidence_threshold"])
    loc = QuestionLocator()
    for q in loc.locate_all(ocr.run(img), img.size[1]):
        print(f"--- 题{q.number} [{q.qtype}] stem={q.stem[:20]!r}")
        for label, (cx, cy) in q.option_centers.items():
            y1, y2 = max(0, cy - 20), cy + 20
            x1, x2 = max(0, cx - 170), cx + 10
            region = arr[y1:y2, x1:x2]
            r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
            blue = ((b > 120) & (b - r > 50) & (b - g > 25))
            col_max = int(blue.sum(axis=0).max()) if blue.size else 0
            state = "选中" if col_max > 20 else "未选"
            print(f"  {label} @({cx},{cy}) 总蓝px={int(blue.sum())} 列最大={col_max} -> {state}")


if __name__ == "__main__":
    main()
